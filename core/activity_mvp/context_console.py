from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import sqlite3
import statistics
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


CONTEXT_RECEIPT_VERSION = "ContextReceiptV1"
CONTEXT_BUNDLE_VERSION = "ContextBundleV1"
MEMORY_CANDIDATE_VERSION = "MemoryCandidateV1"
DELIVERY_RECEIPT_VERSION = "DeliveryReceiptV1"
CONTEXT_SNAPSHOT_VERSION = "ContextSnapshotV1"
OWNER_CONTEXT_ACTION_VERSION = "OwnerContextActionV1"
MEMORY_CANDIDATE_LIST_VERSION = "MemoryCandidateListV1"
DELIVERY_STATUS_VERSION = "DeliveryStatusV1"
LOCAL_HANDOFF_VERSION = "LocalHandoffV1"
PRODUCT_FEEDBACK_VERSION = "ProductFeedbackV1"
PRODUCT_EXPERIMENT_STATUS_VERSION = "ProductExperimentStatusV1"
CONTROLLED_EVIDENCE_VERSION = "ControlledEvidenceV1"
DOCUMENT_MAPPING_CONTRACT_VERSION = "DocumentContextMappingV1"
DOCUMENT_ADAPTER_CONTRACT_VERSION = "DocumentChangeAdapterV2"
DOCUMENT_OWNER_LOCAL_PROJECTION_VERSION = "DocumentOwnerLocalObservationV1"
DOCUMENT_TRUSTED_PROJECTION_VERSION = "DocumentTrustedExportV1"
DOCUMENT_MAPPING_PRINCIPAL_KIND = "document_mapping_internal"
HA_UNVERIFIED_CLUE_PROJECTION_VERSION = "HAUnverifiedClueV1"
CONTEXT_CONSOLE_SCHEMA_VERSION = 31
CONTEXT_FEATURE_FLAG = "context_console_phase1_enabled"
CONTEXT_FEATURE_CONFIRMATION = "ENABLE_CONTEXT_CONSOLE_SOURCE_CANDIDATE"
MAX_CONTRACT_BYTES = 16 * 1024
MAX_JSON_DEPTH = 6
MAX_JSON_ITEMS = 64
MAX_LABEL_LENGTH = 80
MAX_QUERY_LENGTH = 80
RECEIPT_MAX_AGE = timedelta(minutes=15)
RECEIPT_FUTURE_SKEW = timedelta(minutes=2)
UNVERIFIED_RETENTION_DAYS = 2
VERIFIED_RETENTION_DAYS = 90
PRODUCT_WIN_RETENTION_DAYS = 30
PRODUCT_EXPERIMENT_MIN_DAYS = 14
PRODUCT_EXPERIMENT_MAX_DAYS = 21
PRODUCT_EXPERIMENT_MIN_ATTEMPTS = 10
PRODUCT_EXPERIMENT_USEFUL_RATE_THRESHOLD = 0.80
PRODUCT_EXPERIMENT_MEDIAN_RESUME_THRESHOLD_SECONDS = 30.0
PRODUCT_EXPERIMENT_TERMINAL_STATUSES = {"passed", "not_passed"}
PRODUCT_EXPERIMENT_FINALIZE_CONFIRMATION = "FINALIZE_LOCAL_PRODUCT_EXPERIMENT"
CONTEXT_LOCAL_TIMEZONE_SETTING = "context_local_timezone"
DEFAULT_CONTEXT_LOCAL_TIMEZONE = "UTC"
PHASE2_CONFIRMATION_SCAN_LIMIT = 500

PRIVACY_CLASSES = {"private_local", "sensitive_local", "restricted_local"}
LABEL_KINDS = {"project", "workstream", "object", "version", "subject", "target"}
SUBJECT_SCOPES = {"owner", "third_party", "system"}
PRINCIPAL_KINDS = {
    "ai_lifecycle",
    "user_confirmation",
    "file_adapter",
    "git_adapter",
    "external_adapter",
    "bundle_consumer",
    "delivery_provider",
}
LIFECYCLE_EVENTS = {
    "turn_started",
    "waiting_approval",
    "answer_ready",
    "stop",
    "idle",
    "failed",
    "interrupted",
    "status_unknown",
}
CONTEXT_STATES = {
    "intent_observed",
    "in_progress",
    "draft_generated",
    "artifact_saved",
    "artifact_deleted",
    "artifact_committed",
    "external_delivered",
    "completed_confirmed",
    "blocked",
}
CONTROLLED_EVIDENCE_KINDS = {
    "user_confirmation",
    "file_saved",
    "git_commit",
    "external_receipt",
}
DELIVERY_TRANSITIONS = {"consumed", "delivered", "failed"}
DELIVERY_PRINCIPAL_ROLES = {"consumer", "provider"}
DELIVERY_FAILURE_REASONS = {
    "consumer_rejected",
    "provider_rejected",
    "authentication_failed",
    "bundle_hash_mismatch",
    "source_revoked",
    "provider_unavailable",
    "delivery_timeout",
    "unknown_failure",
}
PRODUCT_WIN_OUTCOMES = {"recovered_and_resumed", "handoff_reused", "trusted_context_avoided_rework"}
STABILITY_COMPONENTS = {"capture", "privacy", "storage", "context_fusion"}
NO_RESET_CHANGE_KINDS = {"ui", "copy"}
TARGET_SCOPES = {"local", "external"}
TARGET_EXPORT_POLICIES = {"local_only", "explicit_export"}
EVIDENCE_BINDING_WINDOW = timedelta(hours=24)
MAX_EVIDENCE_REFS = 8
MAX_JSON_INTEGER_DIGITS = 1024
OPERATIONAL_EVIDENCE_KINDS = {"file_saved", "git_commit"}
OWNER_CONTEXT_ACTION_CONFIRMATION = "CONFIRM_OWNER_CONTEXT_ACTION"
OWNER_CONTEXT_ACTIONS = {"confirm_card", "manual_note"}
OWNER_CONTEXT_ANSWERS = {
    "occurred",
    "not_occurred",
    "misrecognized",
    "ignored",
    "defer",
}
PRODUCT_FEEDBACK_SOURCE_KINDS = {"bundle", "local_handoff"}

_CONTEXT_RECEIPT_FIELDS = {
    "contract_version",
    "idempotency_key",
    "producer_id",
    "key_id",
    "source_ref",
    "occurred_at",
    "project_ref",
    "workstream_ref",
    "object_ref",
    "version_ref",
    "subject_ref",
    "privacy_class",
    "lifecycle_event",
    "evidence_refs",
    "signature",
}
_CONTROLLED_EVIDENCE_FIELDS = {
    "contract_version",
    "idempotency_key",
    "principal_id",
    "key_id",
    "source_ref",
    "occurred_at",
    "project_ref",
    "workstream_ref",
    "object_ref",
    "version_ref",
    "subject_ref",
    "privacy_class",
    "evidence_kind",
    "state",
    "provider_receipt_ref",
    "verification_scope",
    "memory_eligible",
    "signature",
}
_DELIVERY_RECEIPT_FIELDS = {
    "contract_version",
    "idempotency_key",
    "principal_id",
    "key_id",
    "delivery_ref",
    "bundle_ref",
    "attempt_no",
    "project_ref",
    "target_ref",
    "target_scope",
    "export_policy",
    "privacy_policy_hash",
    "principal_role",
    "transition",
    "occurred_at",
    "bundle_hash",
    "provider_receipt_ref",
    "failure_reason_code",
    "signature",
}
_CONTEXT_SNAPSHOT_FIELDS = {
    "project_ref",
    "start_at",
    "end_at",
    "query",
    "include_clues",
    "limit",
}
_MEMORY_CANDIDATE_LIST_FIELDS = {"project_ref", "limit"}
_DELIVERY_STATUS_FIELDS = {
    "project_ref",
    "target_ref",
    "start_at",
    "end_at",
    "limit",
}
_LOCAL_HANDOFF_FIELDS = {
    "project_ref",
    "start_at",
    "end_at",
    "query",
    "limit",
}
_OWNER_CONTEXT_ACTION_FIELDS = {
    "contract_version",
    "action",
    "idempotency_key",
    "confirmation_action",
    "confirmation_ref",
    "expected_revision",
    "answer",
    "reason",
    "safe_label",
    "workstream",
    "outcome",
    "started_at",
    "ended_at",
    "project_ref",
    "project_label",
    "workstream_ref",
    "object_ref",
    "version_ref",
    "subject_ref",
    "privacy_class",
    "context_state",
}
_PRODUCT_FEEDBACK_FIELDS = {
    "contract_version",
    "idempotency_key",
    "source_kind",
    "source_ref",
    "project_ref",
    "useful",
    "resume_seconds",
    "experiment_ref",
}

_PREFIXES: dict[str, str] = {
    "idempotency_key": "idem_",
    "producer_id": "principal_",
    "principal_id": "principal_",
    "key_id": "key_",
    "source_ref": "source_",
    "project_ref": "project_",
    "workstream_ref": "workstream_",
    "object_ref": "object_",
    "version_ref": "version_",
    "subject_ref": "subject_",
    "evidence_ref": "evidence_",
    "provider_receipt_ref": "provider_",
    "delivery_ref": "delivery_",
    "bundle_ref": "bundle_",
    "target_ref": "target_",
    "win_ref": "win_",
    "experiment_ref": "experiment_",
    "event_id": "event_",
    "mapping_ref": "mapping_",
    "confirmation_ref": "confirmation_",
    "feedback_ref": "feedback_",
    "handoff_ref": "bundle_",
}
_OPAQUE_SUFFIX_RE = re.compile(r"^[a-f0-9]{8,64}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SIGNATURE_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PATH_OR_URL_RE = re.compile(
    r"(?:https?://|file://|\b[A-Za-z]:[\\/]|\\\\|(?:^|[\s\"'(:=])(?:/[^/\s][^\s]*|~/[^\s]+))",
    re.I,
)
_SECRET_LIKE_RE = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{12,}|\b(?:token|secret|password|cookie)\s*[:=])",
    re.I,
)
_PUBLIC_BUNDLE_FORBIDDEN_KEYS = {
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
    "diagnostic_json",
    "body",
    "frame",
    "deletion_group",
    "consumer_receipt_ref",
    "provider_receipt_ref",
    "failure_reason_code",
}
_PUBLIC_BUNDLE_INTERNAL_PREFIXES = (
    "verified_",
    "candidate_",
    "observation_",
    "evidence_",
    "receipt_",
    "withdrawal_",
)


def _contains_unicode_control(value: str) -> bool:
    """Reject all Cc/Cf code points, including bidi and zero-width controls."""
    return any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)


def _looks_like_json_document(value: str) -> bool:
    candidate = value.strip()
    if not candidate or candidate[0] not in "[{":
        return False
    try:
        decoded = json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(decoded, (dict, list))


def _validate_public_bundle_projection(value: Any, *, depth: int = 0) -> None:
    """Reject internal database identity or raw/private residue before hashing."""
    if depth > MAX_JSON_DEPTH:
        raise ContractError("public_bundle_projection_invalid")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or key in _PUBLIC_BUNDLE_FORBIDDEN_KEYS:
                raise ContractError("public_bundle_internal_field_forbidden")
            _validate_public_bundle_projection(child, depth=depth + 1)
        return
    if isinstance(value, list):
        for child in value:
            _validate_public_bundle_projection(child, depth=depth + 1)
        return
    if isinstance(value, str):
        if value.startswith(_PUBLIC_BUNDLE_INTERNAL_PREFIXES):
            raise ContractError("public_bundle_internal_id_forbidden")
        if (
            _PATH_OR_URL_RE.search(value)
            or _SECRET_LIKE_RE.search(value)
            or _contains_unicode_control(value)
            or _looks_like_json_document(value)
        ):
            raise ContractError("public_bundle_private_value_forbidden")


class ContractError(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _execute_sql_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute a SQL script without sqlite3.executescript()'s implicit commit.

    ``executescript`` commits a pending transaction under Python's legacy
    transaction control.  Context schema migration runs behind a continuous writer
    barrier, so every statement is dispatched individually while the caller's
    explicit transaction remains active.  ``sqlite3.complete_statement`` keeps
    trigger bodies intact even though they contain internal semicolons.
    """

    statement = ""
    for character in script:
        statement += character
        if character == ";" and sqlite3.complete_statement(statement):
            sql = statement.strip()
            statement = ""
            if sql:
                conn.execute(sql)
    if statement.strip():
        raise RuntimeError("incomplete_context_schema_statement")


@dataclass(frozen=True)
class Principal:
    principal_id: str
    key_id: str
    principal_kind: str
    project_ref: str
    target_ref: str
    target_scope: str
    key_fingerprint: str



def _context_authorization_lock(conn: sqlite3.Connection, *, exclusive: bool):
    # Reuse the established authorization lease so context reads and source
    # deletion share one cross-process ordering primitive.  Import lazily to
    # avoid a document-adapter/context-console import cycle.
    from .document_changes import document_authorization_lock

    return document_authorization_lock(
        conn,
        exclusive=exclusive,
        initialize_if_missing=True,
    )

def init_context_console_schema(
    conn: sqlite3.Connection,
    *,
    candidate_rebuild_fault: Callable[[str], None] | None = None,
) -> None:
    """Create only additive schema-31 objects.

    All tables use a ``context_`` prefix so a database that previously ran the
    v0.4 shadow schema 29 can migrate without interpreting or rewriting its
    experimental task-ledger/outbox rows.
    """
    _execute_sql_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS context_label_registry (
          ref TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          label TEXT NOT NULL,
          project_ref TEXT NOT NULL DEFAULT '',
          subject_scope TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL,
          revoked_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_context_labels_kind_status
          ON context_label_registry(kind, status, project_ref);

        CREATE TABLE IF NOT EXISTS context_principals (
          principal_id TEXT NOT NULL,
          key_id TEXT NOT NULL,
          principal_kind TEXT NOT NULL,
          project_ref TEXT NOT NULL DEFAULT '',
          target_ref TEXT NOT NULL DEFAULT '',
          target_scope TEXT NOT NULL DEFAULT '',
          key_fingerprint TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL,
          revoked_at TEXT,
          PRIMARY KEY(principal_id, key_id)
        );
        CREATE INDEX IF NOT EXISTS idx_context_principals_status
          ON context_principals(status, principal_kind, project_ref);

        CREATE TABLE IF NOT EXISTS context_ingest_receipts (
          receipt_ref TEXT PRIMARY KEY,
          contract_type TEXT NOT NULL,
          principal_id TEXT NOT NULL,
          key_id TEXT NOT NULL,
          source_ref TEXT NOT NULL,
          idempotency_key TEXT NOT NULL,
          payload_hash TEXT NOT NULL,
          source_event_ref TEXT NOT NULL DEFAULT '',
          received_at TEXT NOT NULL,
          occurred_at TEXT NOT NULL,
          project_ref TEXT NOT NULL,
          observation_ref TEXT,
          result_status TEXT NOT NULL,
          UNIQUE(contract_type, principal_id, source_ref, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_context_receipts_time
          ON context_ingest_receipts(received_at, contract_type);

        CREATE TABLE IF NOT EXISTS context_external_receipt_bindings (
          principal_id TEXT NOT NULL,
          provider_receipt_ref TEXT NOT NULL,
          semantic_hash TEXT NOT NULL,
          source_ref TEXT NOT NULL,
          project_ref TEXT NOT NULL,
          workstream_ref TEXT NOT NULL,
          object_ref TEXT NOT NULL,
          version_ref TEXT NOT NULL,
          subject_ref TEXT NOT NULL,
          privacy_class TEXT NOT NULL,
          state TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(principal_id, provider_receipt_ref)
        );

        CREATE TABLE IF NOT EXISTS context_observations (
          observation_ref TEXT PRIMARY KEY,
          source_kind TEXT NOT NULL,
          source_ref TEXT NOT NULL,
          source_event_ref TEXT NOT NULL DEFAULT '',
          principal_id TEXT NOT NULL DEFAULT '',
          key_id TEXT NOT NULL DEFAULT '',
          adapter_kind TEXT NOT NULL DEFAULT '',
          receipt_ref TEXT,
          lifecycle_event TEXT NOT NULL DEFAULT '',
          occurred_at TEXT NOT NULL,
          project_ref TEXT NOT NULL,
          workstream_ref TEXT NOT NULL,
          object_ref TEXT NOT NULL,
          version_ref TEXT NOT NULL,
          subject_ref TEXT NOT NULL,
          privacy_class TEXT NOT NULL,
          trust_partition TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          expires_at TEXT NOT NULL,
          created_at TEXT NOT NULL,
          revoked_at TEXT,
          revocation_reason_code TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_context_observations_time
          ON context_observations(occurred_at, status, trust_partition);
        CREATE INDEX IF NOT EXISTS idx_context_observations_source
          ON context_observations(source_ref, status);

        CREATE TABLE IF NOT EXISTS context_evidence (
          evidence_ref TEXT PRIMARY KEY,
          observation_ref TEXT NOT NULL,
          evidence_kind TEXT NOT NULL,
          evidence_tier TEXT NOT NULL,
          state TEXT NOT NULL,
          state_cap TEXT NOT NULL,
          provenance_ref TEXT NOT NULL,
          source_ref TEXT NOT NULL,
          occurred_at TEXT NOT NULL,
          project_ref TEXT NOT NULL DEFAULT '',
          workstream_ref TEXT NOT NULL DEFAULT '',
          object_ref TEXT NOT NULL DEFAULT '',
          version_ref TEXT NOT NULL DEFAULT '',
          subject_ref TEXT NOT NULL DEFAULT '',
          principal_id TEXT NOT NULL DEFAULT '',
          key_id TEXT NOT NULL DEFAULT '',
          adapter_kind TEXT NOT NULL DEFAULT '',
          privacy_class TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          expires_at TEXT NOT NULL,
          created_at TEXT NOT NULL,
          revoked_at TEXT,
          revocation_reason_code TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(observation_ref) REFERENCES context_observations(observation_ref)
        );
        CREATE INDEX IF NOT EXISTS idx_context_evidence_observation
          ON context_evidence(observation_ref, status);

        CREATE TABLE IF NOT EXISTS context_observation_evidence_links (
          observation_ref TEXT NOT NULL,
          evidence_ref TEXT NOT NULL,
          PRIMARY KEY(observation_ref, evidence_ref)
        );

        CREATE TABLE IF NOT EXISTS context_candidates (
          candidate_ref TEXT PRIMARY KEY,
          observation_ref TEXT NOT NULL,
          desired_state TEXT NOT NULL,
          disposition TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          revoked_at TEXT,
          revocation_reason_code TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(observation_ref) REFERENCES context_observations(observation_ref)
        );
        CREATE INDEX IF NOT EXISTS idx_context_candidates_observation
          ON context_candidates(observation_ref, status, disposition);

        CREATE TABLE IF NOT EXISTS context_candidate_evidence (
          candidate_ref TEXT NOT NULL,
          evidence_ref TEXT NOT NULL,
          dependency_role TEXT NOT NULL DEFAULT 'required',
          PRIMARY KEY(candidate_ref, evidence_ref)
        );

        CREATE TABLE IF NOT EXISTS context_verified (
          verified_ref TEXT PRIMARY KEY,
          candidate_ref TEXT NOT NULL,
          observation_ref TEXT NOT NULL,
          project_ref TEXT NOT NULL,
          workstream_ref TEXT NOT NULL,
          object_ref TEXT NOT NULL,
          version_ref TEXT NOT NULL,
          subject_ref TEXT NOT NULL,
          state TEXT NOT NULL,
          evidence_tier TEXT NOT NULL,
          source_kind TEXT NOT NULL,
          source_ref TEXT NOT NULL,
          provenance_ref TEXT NOT NULL,
          privacy_class TEXT NOT NULL,
          verification_scope TEXT NOT NULL DEFAULT 'operational',
          memory_eligible INTEGER NOT NULL DEFAULT 0,
          valid_from TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL,
          revoked_at TEXT,
          revocation_reason_code TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(candidate_ref) REFERENCES context_candidates(candidate_ref),
          FOREIGN KEY(observation_ref) REFERENCES context_observations(observation_ref)
        );
        CREATE INDEX IF NOT EXISTS idx_context_verified_filter
          ON context_verified(project_ref, valid_from, status, expires_at);
        CREATE INDEX IF NOT EXISTS idx_context_verified_observation
          ON context_verified(observation_ref, status);

        CREATE VIRTUAL TABLE IF NOT EXISTS context_search_index USING fts5(
          verified_ref UNINDEXED,
          project_ref UNINDEXED,
          occurred_at UNINDEXED,
          project_label,
          workstream_label,
          object_label,
          version_label,
          state
        );

        CREATE TABLE IF NOT EXISTS context_memory_candidates (
          memory_candidate_ref TEXT PRIMARY KEY,
          verified_ref TEXT NOT NULL,
          subject_ref TEXT NOT NULL,
          project_ref TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'candidate_only',
          created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          revoked_at TEXT,
          revocation_reason_code TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(verified_ref) REFERENCES context_verified(verified_ref)
        );
        CREATE INDEX IF NOT EXISTS idx_context_memory_verified
          ON context_memory_candidates(verified_ref, status);

        CREATE TABLE IF NOT EXISTS context_bundle_manifests (
          bundle_ref TEXT PRIMARY KEY,
          bundle_hash TEXT NOT NULL,
          project_ref TEXT NOT NULL DEFAULT '',
          start_at TEXT NOT NULL DEFAULT '',
          end_at TEXT NOT NULL DEFAULT '',
          query_fingerprint TEXT NOT NULL DEFAULT '',
          target_ref TEXT NOT NULL DEFAULT '',
          item_count INTEGER NOT NULL DEFAULT 0,
          stable_projection_json TEXT NOT NULL DEFAULT '{}',
          frozen_payload_json TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL,
          restricted_at TEXT,
          restriction_reason_code TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_context_bundle_hash
          ON context_bundle_manifests(bundle_hash, status);

        CREATE TABLE IF NOT EXISTS context_bundle_members (
          bundle_ref TEXT NOT NULL,
          member_kind TEXT NOT NULL,
          member_ref TEXT NOT NULL,
          member_hash TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          revoked_at TEXT,
          revocation_reason_code TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(bundle_ref, member_kind, member_ref),
          FOREIGN KEY(bundle_ref) REFERENCES context_bundle_manifests(bundle_ref)
        );
        CREATE INDEX IF NOT EXISTS idx_context_bundle_member_ref
          ON context_bundle_members(member_ref, status);

        CREATE TABLE IF NOT EXISTS context_deliveries (
          delivery_ref TEXT PRIMARY KEY,
          bundle_ref TEXT NOT NULL,
          bundle_hash TEXT NOT NULL,
          project_ref TEXT NOT NULL DEFAULT '',
          target_ref TEXT NOT NULL,
          attempt_no INTEGER NOT NULL DEFAULT 1,
          target_scope TEXT NOT NULL DEFAULT 'local',
          export_policy TEXT NOT NULL DEFAULT 'local_only',
          allowed_privacy_classes_json TEXT NOT NULL DEFAULT '[]',
          privacy_policy_hash TEXT NOT NULL DEFAULT '',
          state TEXT NOT NULL,
          prepared_at TEXT NOT NULL,
          consumed_at TEXT,
          delivered_at TEXT,
          failed_at TEXT,
          consumer_receipt_ref TEXT NOT NULL DEFAULT '',
          provider_receipt_ref TEXT NOT NULL DEFAULT '',
          failure_reason_code TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(bundle_ref) REFERENCES context_bundle_manifests(bundle_ref),
          UNIQUE(bundle_ref, target_ref, attempt_no)
        );
        CREATE INDEX IF NOT EXISTS idx_context_deliveries_bundle
          ON context_deliveries(bundle_ref, state);

        CREATE TABLE IF NOT EXISTS context_delivery_receipts (
          receipt_ref TEXT PRIMARY KEY,
          delivery_ref TEXT NOT NULL,
          principal_id TEXT NOT NULL,
          key_id TEXT NOT NULL,
          idempotency_key TEXT NOT NULL,
          payload_hash TEXT NOT NULL,
          bundle_ref TEXT NOT NULL DEFAULT '',
          attempt_no INTEGER NOT NULL DEFAULT 0,
          project_ref TEXT NOT NULL DEFAULT '',
          target_ref TEXT NOT NULL DEFAULT '',
          target_scope TEXT NOT NULL DEFAULT '',
          export_policy TEXT NOT NULL DEFAULT '',
          privacy_policy_hash TEXT NOT NULL DEFAULT '',
          principal_role TEXT NOT NULL DEFAULT '',
          transition TEXT NOT NULL,
          occurred_at TEXT NOT NULL,
          bundle_hash TEXT NOT NULL,
          provider_receipt_ref TEXT NOT NULL DEFAULT '',
          failure_reason_code TEXT NOT NULL DEFAULT '',
          result_state TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(delivery_ref) REFERENCES context_deliveries(delivery_ref),
          UNIQUE(delivery_ref, principal_id, idempotency_key)
        );

        CREATE TABLE IF NOT EXISTS context_withdrawal_receipts (
          withdrawal_ref TEXT PRIMARY KEY,
          observation_ref TEXT NOT NULL,
          bundle_ref TEXT NOT NULL,
          delivery_ref TEXT NOT NULL,
          reason_code TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(observation_ref, delivery_ref)
        );

        CREATE TABLE IF NOT EXISTS context_source_events (
          source_event_ref TEXT PRIMARY KEY,
          contract_type TEXT NOT NULL,
          principal_id TEXT NOT NULL,
          key_id TEXT NOT NULL,
          source_ref TEXT NOT NULL,
          occurred_at TEXT NOT NULL,
          project_ref TEXT NOT NULL,
          workstream_ref TEXT NOT NULL,
          object_ref TEXT NOT NULL,
          version_ref TEXT NOT NULL,
          subject_ref TEXT NOT NULL,
          payload_hash TEXT NOT NULL,
          observation_ref TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL,
          tombstoned_at TEXT,
          tombstone_reason_code TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_context_source_events_source
          ON context_source_events(principal_id, source_ref, occurred_at, status);

        CREATE TABLE IF NOT EXISTS context_source_event_tombstones (
          source_event_ref TEXT PRIMARY KEY,
          principal_id TEXT NOT NULL,
          source_ref TEXT NOT NULL,
          observation_ref TEXT NOT NULL DEFAULT '',
          reason_code TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS context_document_mappings (
          mapping_ref TEXT NOT NULL,
          mapping_version INTEGER NOT NULL,
          contract_version TEXT NOT NULL,
          root_id TEXT NOT NULL,
          project_ref TEXT NOT NULL,
          workstream_ref TEXT NOT NULL,
          object_ref TEXT NOT NULL,
          version_ref TEXT NOT NULL,
          subject_ref TEXT NOT NULL,
          principal_id TEXT NOT NULL,
          key_id TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          effective_at TEXT NOT NULL,
          expires_at TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          revoked_at TEXT,
          revocation_reason_code TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(mapping_ref, mapping_version)
        );
        CREATE INDEX IF NOT EXISTS idx_context_document_mapping_root
          ON context_document_mappings(root_id, status, mapping_version);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_context_document_mapping_one_active_root
          ON context_document_mappings(root_id) WHERE status='active';

        CREATE TABLE IF NOT EXISTS context_document_source_versions (
          document_source_ref TEXT PRIMARY KEY,
          source_event_ref TEXT NOT NULL,
          root_id TEXT NOT NULL,
          mapping_ref TEXT NOT NULL,
          mapping_version INTEGER NOT NULL,
          document_identity_ref TEXT NOT NULL,
          helper_event_ref TEXT NOT NULL,
          content_version_ref TEXT NOT NULL,
          observation_ref TEXT NOT NULL,
          evidence_ref TEXT NOT NULL,
          candidate_ref TEXT NOT NULL,
          change_kind TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL,
          revoked_at TEXT,
          revocation_reason_code TEXT NOT NULL DEFAULT '',
          UNIQUE(root_id, helper_event_ref, content_version_ref, change_kind)
        );
        CREATE INDEX IF NOT EXISTS idx_context_document_source_root
          ON context_document_source_versions(root_id, document_identity_ref, status);
        CREATE INDEX IF NOT EXISTS idx_context_document_source_observation
          ON context_document_source_versions(observation_ref, status);

        CREATE TABLE IF NOT EXISTS context_document_tombstones (
          tombstone_ref TEXT PRIMARY KEY,
          scope TEXT NOT NULL,
          root_id TEXT NOT NULL,
          document_identity_ref TEXT NOT NULL DEFAULT '',
          reason_code TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL,
          UNIQUE(scope, root_id, document_identity_ref)
        );

        CREATE TABLE IF NOT EXISTS context_target_policies (
          target_ref TEXT PRIMARY KEY,
          target_scope TEXT NOT NULL DEFAULT '',
          allowed_privacy_classes TEXT NOT NULL DEFAULT '[]',
          export_policy TEXT NOT NULL DEFAULT '',
          policy_declared INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS context_product_experiments (
          experiment_ref TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          started_local_date TEXT,
          ended_local_date TEXT,
          minimum_days INTEGER NOT NULL DEFAULT 14,
          maximum_days INTEGER NOT NULL DEFAULT 21,
          timezone_name TEXT NOT NULL DEFAULT 'UTC',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS context_product_experiment_attempts (
          attempt_ref TEXT PRIMARY KEY,
          experiment_ref TEXT NOT NULL,
          event_id TEXT NOT NULL,
          local_date TEXT NOT NULL,
          useful INTEGER,
          outcome TEXT,
          resume_seconds REAL,
          source_ref TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(experiment_ref) REFERENCES context_product_experiments(experiment_ref),
          UNIQUE(experiment_ref, event_id),
          UNIQUE(experiment_ref, source_ref)
        );
        CREATE INDEX IF NOT EXISTS idx_context_experiment_attempts
          ON context_product_experiment_attempts(experiment_ref, local_date);

        CREATE TABLE IF NOT EXISTS context_product_wins (
          win_ref TEXT PRIMARY KEY,
          occurred_at TEXT NOT NULL,
          local_date TEXT NOT NULL,
          project_ref TEXT NOT NULL,
          outcome_code TEXT NOT NULL,
          minutes_saved INTEGER NOT NULL DEFAULT 0,
          source_ref TEXT NOT NULL,
          created_at TEXT NOT NULL,
          expires_local_date TEXT NOT NULL,
          timezone_name TEXT NOT NULL DEFAULT 'UTC',
          expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_context_product_wins_expiry
          ON context_product_wins(expires_at, occurred_at);

        CREATE TABLE IF NOT EXISTS context_component_baselines (
          component TEXT PRIMARY KEY,
          version_ref TEXT NOT NULL,
          generation INTEGER NOT NULL,
          reset_reason_code TEXT NOT NULL,
          recorded_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS context_activity_confirmation_links (
          link_ref TEXT PRIMARY KEY,
          confirmation_ref TEXT NOT NULL,
          confirmation_kind TEXT NOT NULL,
          confirmation_key TEXT NOT NULL,
          confirmation_revision TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE,
          payload_hash TEXT NOT NULL,
          answer TEXT NOT NULL,
          observation_ref TEXT NOT NULL DEFAULT '',
          evidence_ref TEXT NOT NULL DEFAULT '',
          candidate_ref TEXT NOT NULL DEFAULT '',
          verified_ref TEXT NOT NULL DEFAULT '',
          public_context_ref TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          revoked_at TEXT,
          revocation_reason_code TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_context_confirmation_links_observation
          ON context_activity_confirmation_links(observation_ref, status);
        CREATE INDEX IF NOT EXISTS idx_context_confirmation_links_key
          ON context_activity_confirmation_links(confirmation_kind, confirmation_key, status);
        CREATE INDEX IF NOT EXISTS idx_context_confirmation_links_history
          ON context_activity_confirmation_links(confirmation_ref, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_context_confirmation_links_active_once
          ON context_activity_confirmation_links(confirmation_ref)
          WHERE status IN ('verified','non_verifying');

        CREATE TABLE IF NOT EXISTS context_product_feedback (
          feedback_ref TEXT PRIMARY KEY,
          source_kind TEXT NOT NULL,
          source_ref TEXT NOT NULL,
          bundle_ref TEXT NOT NULL,
          bundle_hash TEXT NOT NULL,
          project_ref TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE,
          payload_hash TEXT NOT NULL,
          useful INTEGER NOT NULL,
          resume_seconds REAL,
          experiment_ref TEXT NOT NULL DEFAULT '',
          win_ref TEXT NOT NULL DEFAULT '',
          occurred_at TEXT NOT NULL,
          local_date TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(source_ref)
        );
        CREATE INDEX IF NOT EXISTS idx_context_product_feedback_project_time
          ON context_product_feedback(project_ref, occurred_at);
        """
    )
    duplicate_feedback_source = conn.execute(
        "SELECT source_ref FROM context_product_feedback "
        "GROUP BY source_ref HAVING COUNT(*)>1 LIMIT 1"
    ).fetchone()
    if duplicate_feedback_source:
        raise sqlite3.DatabaseError(
            "context_product_feedback_source_scope_conflict"
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_context_product_feedback_source_once "
        "ON context_product_feedback(source_ref)"
    )
    now = _utc_now()
    # The stored zone exists before the v2 corrective migration so legacy
    # content-free wins can be converted from elapsed UTC hours to a stable
    # local-calendar boundary without consulting the host process timezone.
    conn.execute(
        "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO NOTHING",
        (CONTEXT_LOCAL_TIMEZONE_SETTING, DEFAULT_CONTEXT_LOCAL_TIMEZONE, now),
    )
    _upgrade_context_v2_schema(
        conn,
        fault_injector=candidate_rebuild_fault,
    )
    conn.execute(
        "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO NOTHING",
        (CONTEXT_FEATURE_FLAG, "false", now),
    )
    # These gates are deliberately not rollout switches in this candidate.
    # A schema-29 shadow database may already contain permissive values, so
    # every schema-31 initialization forces them back to their safe state.
    forced_off = {
        "context_memory_writer_enabled": "false",
        "context_source_revocation_cascade_ready": "false",
    }
    for key, value in forced_off.items():
        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at "
            "WHERE settings.value<>excluded.value",
            (key, value, now),
        )



def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"] if hasattr(row, "keys") else row[1])
        for row in conn.execute(f"PRAGMA table_xinfo({table})")
    }


def _ensure_context_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    if name not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _index_contracts(
    conn: sqlite3.Connection,
    table: str,
) -> list[tuple[tuple[str, ...], bool, bool, str]]:
    output: list[tuple[tuple[str, ...], bool, bool, str]] = []
    for row in conn.execute(f"PRAGMA index_list({table})"):
        unique = bool(int(row["unique"] if hasattr(row, "keys") else row[2]))
        name = str(row["name"] if hasattr(row, "keys") else row[1])
        origin = str(row["origin"] if hasattr(row, "keys") else row[3])
        partial = bool(int(row["partial"] if hasattr(row, "keys") else row[4]))
        escaped = name.replace("'", "''")
        columns = tuple(
            str(item["name"] if hasattr(item, "keys") else item[2])
            for item in conn.execute(f"PRAGMA index_info('{escaped}')")
        )
        output.append((columns, unique, partial, origin))
    return output


def _index_key_term_contract(
    conn: sqlite3.Connection,
    index_name: str,
) -> tuple[tuple[str, int, str, int], ...]:
    escaped = index_name.replace("'", "''")
    return tuple(
        (
            str(row["name"] if hasattr(row, "keys") else row[2]),
            int(row["desc"] if hasattr(row, "keys") else row[3]),
            str(row["coll"] if hasattr(row, "keys") else row[4]).upper(),
            int(row["key"] if hasattr(row, "keys") else row[5]),
        )
        for row in conn.execute(f"PRAGMA index_xinfo('{escaped}')")
        if int(row["key"] if hasattr(row, "keys") else row[5])
    )


def _table_declares_collation(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    sql = str((row["sql"] if hasattr(row, "keys") else row[0]) or "") if row else ""
    return bool(re.search(r"\bCOLLATE\b", sql, flags=re.IGNORECASE))


_INGEST_RECEIPT_COLUMN_CONTRACT = (
    ("receipt_ref", "TEXT", 0, None, 1, 0),
    ("contract_type", "TEXT", 1, None, 0, 0),
    ("principal_id", "TEXT", 1, None, 0, 0),
    ("key_id", "TEXT", 1, None, 0, 0),
    ("source_ref", "TEXT", 1, None, 0, 0),
    ("idempotency_key", "TEXT", 1, None, 0, 0),
    ("payload_hash", "TEXT", 1, None, 0, 0),
    ("source_event_ref", "TEXT", 1, "''", 0, 0),
    ("received_at", "TEXT", 1, None, 0, 0),
    ("occurred_at", "TEXT", 1, None, 0, 0),
    ("project_ref", "TEXT", 1, None, 0, 0),
    ("observation_ref", "TEXT", 0, None, 0, 0),
    ("result_status", "TEXT", 1, None, 0, 0),
)


_EXTERNAL_BINDING_COLUMN_CONTRACT = (
    ("principal_id", "TEXT", 1, None, 1, 0),
    ("provider_receipt_ref", "TEXT", 1, None, 2, 0),
    ("semantic_hash", "TEXT", 1, None, 0, 0),
    ("source_ref", "TEXT", 1, None, 0, 0),
    ("project_ref", "TEXT", 1, None, 0, 0),
    ("workstream_ref", "TEXT", 1, None, 0, 0),
    ("object_ref", "TEXT", 1, None, 0, 0),
    ("version_ref", "TEXT", 1, None, 0, 0),
    ("subject_ref", "TEXT", 1, None, 0, 0),
    ("privacy_class", "TEXT", 1, None, 0, 0),
    ("state", "TEXT", 1, None, 0, 0),
    ("created_at", "TEXT", 1, None, 0, 0),
)


def _ordered_column_contract(
    conn: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, str, int, Any, int, int], ...]:
    return tuple(
        (
            str(row["name"] if hasattr(row, "keys") else row[1]),
            str(row["type"] if hasattr(row, "keys") else row[2]).upper(),
            int(row["notnull"] if hasattr(row, "keys") else row[3]),
            row["dflt_value"] if hasattr(row, "keys") else row[4],
            int(row["pk"] if hasattr(row, "keys") else row[5]),
            int(row["hidden"] if hasattr(row, "keys") else row[6]),
        )
        for row in conn.execute(f"PRAGMA table_xinfo({table})")
    )


def _ingest_receipt_column_contract_is_exact(conn: sqlite3.Connection) -> bool:
    return (
        _ordered_column_contract(conn, "context_ingest_receipts")
        == _INGEST_RECEIPT_COLUMN_CONTRACT
    )


def _external_binding_table_contract_is_exact(conn: sqlite3.Connection) -> bool:
    if (
        _ordered_column_contract(conn, "context_external_receipt_bindings")
        != _EXTERNAL_BINDING_COLUMN_CONTRACT
    ):
        return False
    expected_key = ("principal_id", "provider_receipt_ref")
    contracts = _index_contracts(conn, "context_external_receipt_bindings")
    expected = [
        (columns, unique, partial, origin)
        for columns, unique, partial, origin in contracts
        if columns == expected_key
    ]
    if len(expected) != 1 or expected[0][1:] != (True, False, "pk"):
        return False
    if any(
        unique and columns != expected_key
        for columns, unique, _partial, _origin in contracts
    ):
        return False
    pk_indexes = [
        str(row["name"] if hasattr(row, "keys") else row[1])
        for row in conn.execute(
            "PRAGMA index_list(context_external_receipt_bindings)"
        )
        if str(row["origin"] if hasattr(row, "keys") else row[3]) == "pk"
    ]
    if len(pk_indexes) != 1:
        return False
    if _index_key_term_contract(conn, pk_indexes[0]) != (
        ("principal_id", 0, "BINARY", 1),
        ("provider_receipt_ref", 0, "BINARY", 1),
    ):
        return False
    return not _table_declares_collation(
        conn,
        "context_external_receipt_bindings",
    )


def _canonicalize_external_binding_rows(
    conn: sqlite3.Connection,
) -> list[tuple[str, str, str, dict[str, str], str]]:
    expected_columns = {item[0] for item in _EXTERNAL_BINDING_COLUMN_CONTRACT}
    xinfo_rows = list(
        conn.execute("PRAGMA table_xinfo(context_external_receipt_bindings)")
    )
    actual_hidden = {
        str(row["name"] if hasattr(row, "keys") else row[1]): int(
            row["hidden"] if hasattr(row, "keys") else row[6]
        )
        for row in xinfo_rows
    }
    actual_columns = set(actual_hidden)
    missing_columns = expected_columns - actual_columns
    unsafe_extra_columns = {
        name
        for name in actual_columns - expected_columns
        if actual_hidden[name] not in {2, 3}
    }
    has_rows = conn.execute(
        "SELECT 1 FROM context_external_receipt_bindings LIMIT 1"
    ).fetchone()
    if missing_columns or unsafe_extra_columns:
        if has_rows:
            raise sqlite3.DatabaseError(
                "external_receipt_binding_schema_unrecoverable"
            )
        return []
    selected_columns = ",".join(item[0] for item in _EXTERNAL_BINDING_COLUMN_CONTRACT)
    rows = conn.execute(
        f"SELECT {selected_columns} FROM context_external_receipt_bindings"
    ).fetchall()
    grouped: dict[
        tuple[str, str],
        tuple[str, dict[str, str], str],
    ] = {}
    for row in rows:
        principal_id = str(row["principal_id"] or "")
        provider_receipt_ref = str(row["provider_receipt_ref"] or "")
        created_at = str(row["created_at"] or "")
        try:
            _validate_opaque_ref("principal_id", principal_id)
            _validate_opaque_ref("provider_receipt_ref", provider_receipt_ref)
            if not created_at:
                raise ContractError("invalid_occurred_at")
        except ContractError as exc:
            raise sqlite3.DatabaseError(
                "external_receipt_binding_history_conflict"
            ) from exc
        binding = {
            field: str(row[field] or "")
            for field in (
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
        semantic_hash = hashlib.sha256(_canonical_bytes(binding)).hexdigest()
        if not hmac.compare_digest(str(row["semantic_hash"] or ""), semantic_hash):
            raise sqlite3.DatabaseError(
                "external_receipt_binding_history_conflict"
            )
        key = (principal_id, provider_receipt_ref)
        existing = grouped.get(key)
        if existing:
            existing_hash, existing_binding, existing_created_at = existing
            if (
                not hmac.compare_digest(existing_hash, semantic_hash)
                or existing_binding != binding
            ):
                raise sqlite3.DatabaseError(
                    "external_receipt_binding_history_conflict"
                )
            grouped[key] = (
                existing_hash,
                existing_binding,
                min(existing_created_at, created_at),
            )
        else:
            grouped[key] = (semantic_hash, binding, created_at)
    return [
        (principal_id, provider_receipt_ref, semantic_hash, binding, created_at)
        for (principal_id, provider_receipt_ref), (
            semantic_hash,
            binding,
            created_at,
        ) in sorted(grouped.items())
    ]


def _rebuild_external_receipt_bindings(
    conn: sqlite3.Connection,
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> None:
    legacy_table = "context_external_receipt_bindings_scope_v2"
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (legacy_table,),
    ).fetchone():
        raise sqlite3.DatabaseError("ambiguous_external_receipt_binding_rebuild")
    canonical_rows = _canonicalize_external_binding_rows(conn)
    conn.execute("SAVEPOINT context_external_receipt_binding_rebuild")
    try:
        _inject_candidate_rebuild_fault(
            fault_injector, "context_external_receipt_bindings", "before_rename"
        )
        conn.execute(
            f"ALTER TABLE context_external_receipt_bindings RENAME TO {legacy_table}"
        )
        _inject_candidate_rebuild_fault(
            fault_injector, "context_external_receipt_bindings", "after_rename"
        )
        conn.execute(
            "CREATE TABLE context_external_receipt_bindings ("
            "principal_id TEXT NOT NULL,provider_receipt_ref TEXT NOT NULL,"
            "semantic_hash TEXT NOT NULL,source_ref TEXT NOT NULL,project_ref TEXT NOT NULL,"
            "workstream_ref TEXT NOT NULL,object_ref TEXT NOT NULL,version_ref TEXT NOT NULL,"
            "subject_ref TEXT NOT NULL,privacy_class TEXT NOT NULL,state TEXT NOT NULL,"
            "created_at TEXT NOT NULL,PRIMARY KEY(principal_id,provider_receipt_ref))"
        )
        for principal_id, provider_receipt_ref, semantic_hash, binding, created_at in canonical_rows:
            conn.execute(
                "INSERT INTO context_external_receipt_bindings("
                "principal_id,provider_receipt_ref,semantic_hash,source_ref,project_ref,"
                "workstream_ref,object_ref,version_ref,subject_ref,privacy_class,state,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    principal_id,
                    provider_receipt_ref,
                    semantic_hash,
                    binding["source_ref"],
                    binding["project_ref"],
                    binding["workstream_ref"],
                    binding["object_ref"],
                    binding["version_ref"],
                    binding["subject_ref"],
                    binding["privacy_class"],
                    binding["state"],
                    created_at,
                ),
            )
        _backfill_external_receipt_bindings(conn)
        conn.execute(f"DROP TABLE {legacy_table}")
        conn.execute("RELEASE SAVEPOINT context_external_receipt_binding_rebuild")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT context_external_receipt_binding_rebuild")
        conn.execute("RELEASE SAVEPOINT context_external_receipt_binding_rebuild")
        raise


def _upgrade_external_receipt_bindings(
    conn: sqlite3.Connection,
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> None:
    if not _external_binding_table_contract_is_exact(conn):
        _rebuild_external_receipt_bindings(
            conn,
            fault_injector=fault_injector,
        )
    _backfill_external_receipt_bindings(conn)


def _recover_ingest_receipt_source_ref(
    conn: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    old_columns: set[str],
) -> str:
    if "source_ref" in old_columns:
        existing = str(row["source_ref"] or "")
        if existing:
            try:
                _validate_opaque_ref("source_ref", existing)
                return existing
            except ContractError:
                pass

    contract_type = str(row["contract_type"] or "")
    principal_id = str(row["principal_id"] or "")
    source_event_ref = str(row["source_event_ref"] or "")
    if source_event_ref:
        candidates = conn.execute(
            "SELECT source_ref FROM context_source_events "
            "WHERE source_event_ref=? AND contract_type=? AND principal_id=?",
            (source_event_ref, contract_type, principal_id),
        ).fetchall()
    else:
        where = ["contract_type=?", "principal_id=?"]
        params: list[Any] = [contract_type, principal_id]
        observation_ref = str(row["observation_ref"] or "")
        if observation_ref:
            where.append("observation_ref=?")
            params.append(observation_ref)
        else:
            where.extend(("key_id=?", "occurred_at=?", "project_ref=?"))
            params.extend(
                (
                    str(row["key_id"] or ""),
                    str(row["occurred_at"] or ""),
                    str(row["project_ref"] or ""),
                )
            )
        candidates = conn.execute(
            "SELECT source_ref FROM context_source_events WHERE " + " AND ".join(where),
            tuple(params),
        ).fetchall()
    recovered = {str(item["source_ref"] or "") for item in candidates if str(item["source_ref"] or "")}
    if len(recovered) != 1:
        reason = (
            "context_ingest_source_ref_ambiguous"
            if len(recovered) > 1
            else "context_ingest_source_ref_unrecoverable"
        )
        raise sqlite3.DatabaseError(reason)
    source_ref = next(iter(recovered))
    try:
        _validate_opaque_ref("source_ref", source_ref)
    except ContractError as exc:
        raise sqlite3.DatabaseError("context_ingest_source_ref_unrecoverable") from exc
    return source_ref


def _rebuild_context_ingest_receipts_scope(
    conn: sqlite3.Connection,
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> None:
    """Atomically repair the v2 receipt scope without relying on ``.git`` or a schema bump."""
    legacy_table = "context_ingest_receipts_scope_v2"
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (legacy_table,)
    ).fetchone():
        raise sqlite3.DatabaseError("ambiguous_context_ingest_receipts_rebuild")
    old_columns = _table_columns(conn, "context_ingest_receipts")
    if not old_columns:
        return
    expected_columns = {item[0] for item in _INGEST_RECEIPT_COLUMN_CONTRACT}
    missing_other_than_source = (expected_columns - {"source_ref"}) - old_columns
    if missing_other_than_source:
        raise sqlite3.DatabaseError("context_ingest_receipt_columns_unrecoverable")
    if old_columns - expected_columns:
        raise sqlite3.DatabaseError("context_ingest_receipt_columns_unsupported")
    conn.execute("SAVEPOINT context_ingest_scope_rebuild")
    try:
        _inject_candidate_rebuild_fault(
            fault_injector, "context_ingest_receipts", "before_rename"
        )
        conn.execute(f"ALTER TABLE context_ingest_receipts RENAME TO {legacy_table}")
        _inject_candidate_rebuild_fault(
            fault_injector, "context_ingest_receipts", "after_rename"
        )
        _inject_candidate_rebuild_fault(
            fault_injector, "context_ingest_receipts", "before_create"
        )
        conn.execute(
            "CREATE TABLE context_ingest_receipts ("
            "receipt_ref TEXT PRIMARY KEY,contract_type TEXT NOT NULL,principal_id TEXT NOT NULL,"
            "key_id TEXT NOT NULL,source_ref TEXT NOT NULL,idempotency_key TEXT NOT NULL,"
            "payload_hash TEXT NOT NULL,source_event_ref TEXT NOT NULL DEFAULT '',received_at TEXT NOT NULL,"
            "occurred_at TEXT NOT NULL,project_ref TEXT NOT NULL,observation_ref TEXT,result_status TEXT NOT NULL,"
            "UNIQUE(contract_type,principal_id,source_ref,idempotency_key))"
        )
        _inject_candidate_rebuild_fault(
            fault_injector, "context_ingest_receipts", "after_create"
        )
        rows = conn.execute(
            f"SELECT * FROM {legacy_table} ORDER BY received_at,receipt_ref"
        ).fetchall()
        _inject_candidate_rebuild_fault(
            fault_injector, "context_ingest_receipts", "before_copy"
        )
        for row in rows:
            recovered_source_ref = _recover_ingest_receipt_source_ref(
                conn, row=row, old_columns=old_columns
            )
            try:
                conn.execute(
                    "INSERT INTO context_ingest_receipts("
                    "receipt_ref,contract_type,principal_id,key_id,source_ref,idempotency_key,payload_hash,"
                    "source_event_ref,received_at,occurred_at,project_ref,observation_ref,result_status) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["receipt_ref"],
                        row["contract_type"],
                        row["principal_id"],
                        row["key_id"],
                        recovered_source_ref,
                        row["idempotency_key"],
                        row["payload_hash"],
                        row["source_event_ref"],
                        row["received_at"],
                        row["occurred_at"],
                        row["project_ref"],
                        row["observation_ref"],
                        row["result_status"],
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise sqlite3.DatabaseError("context_ingest_scope_recovery_conflict") from exc
        _inject_candidate_rebuild_fault(
            fault_injector, "context_ingest_receipts", "after_copy"
        )
        _inject_candidate_rebuild_fault(
            fault_injector, "context_ingest_receipts", "before_drop"
        )
        conn.execute(f"DROP TABLE {legacy_table}")
        _inject_candidate_rebuild_fault(
            fault_injector, "context_ingest_receipts", "after_drop"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_context_receipts_time "
            "ON context_ingest_receipts(received_at,contract_type)"
        )
        conn.execute("RELEASE SAVEPOINT context_ingest_scope_rebuild")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT context_ingest_scope_rebuild")
        conn.execute("RELEASE SAVEPOINT context_ingest_scope_rebuild")
        raise


def _validated_context_v2_migration_timezone(conn: sqlite3.Connection) -> str:
    """Validate every pre-existing timezone before the corrective migration writes."""
    configured_timezone = _context_timezone_name(conn)
    experiment_columns = _table_columns(conn, "context_product_experiments")
    if "timezone_name" in experiment_columns:
        snapshots = conn.execute(
            "SELECT DISTINCT timezone_name FROM context_product_experiments "
            "WHERE timezone_name IS NOT NULL AND timezone_name<>''"
        ).fetchall()
        for snapshot in snapshots:
            try:
                _validate_timezone_name(str(snapshot[0]))
            except ContractError as exc:
                raise ContractError("invalid_experiment_timezone") from exc
    win_columns = _table_columns(conn, "context_product_wins")
    if "timezone_name" in win_columns:
        snapshots = conn.execute(
            "SELECT DISTINCT timezone_name FROM context_product_wins "
            "WHERE timezone_name IS NOT NULL AND timezone_name<>''"
        ).fetchall()
        for snapshot in snapshots:
            try:
                _validate_timezone_name(str(snapshot[0]))
            except ContractError as exc:
                raise ContractError("invalid_product_win_timezone") from exc
    return configured_timezone

def configure_context_local_timezone(
    conn: sqlite3.Connection,
    *,
    timezone_name: str,
) -> dict[str, Any]:
    """Persist the IANA zone used for all Context Console local-day rules."""
    try:
        normalized = _validate_timezone_name(timezone_name)
    except ContractError as exc:
        return _result("rejected", exc.reason_code)
    now = _utc_now()
    conn.execute(
        "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (CONTEXT_LOCAL_TIMEZONE_SETTING, normalized, now),
    )
    conn.commit()
    return {
        "status": "configured",
        "timezone_name": normalized,
        "local_day_semantics": "iana_calendar_day",
        "raw_access": False,
    }

def context_local_timezone_status(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        timezone_name = _context_timezone_name(conn)
    except ContractError as exc:
        return _result("unknown", exc.reason_code)
    return {
        "status": "available",
        "timezone_name": timezone_name,
        "local_day_semantics": "iana_calendar_day",
        "raw_access": False,
    }

def _repair_candidate_receipt_table(
    conn: sqlite3.Connection,
    *,
    table: str,
    shadow_table: str,
    legacy_primary_key: list[str],
    current_primary_key: list[str],
    legacy_columns: set[str],
    current_columns: set[str],
    create_sql: str,
    copy_sql: str,
    old_projection: str,
    new_projection: str,
    required_unique: tuple[str, ...],
    post_create_sql: str,
    fault_injector: Callable[[str], None] | None,
) -> None:
    columns = _table_columns(conn, table)
    shadow_exists = _context_table_exists(conn, shadow_table)
    if not columns:
        if shadow_exists:
            raise RuntimeError(f"{table}_rebuild_ambiguous_shadow")
        return
    primary_key = _context_table_primary_key(conn, table)
    if primary_key == current_primary_key:
        if shadow_exists:
            raise RuntimeError(f"{table}_rebuild_ambiguous_shadow")
        if columns != current_columns:
            raise RuntimeError(f"{table}_current_shape_invalid")
        if not _context_unique_index_matches(conn, table, required_unique):
            raise RuntimeError(f"{table}_current_unique_scope_invalid")
        return
    if primary_key != legacy_primary_key or columns != legacy_columns:
        # Unknown legacy columns would be silently discarded by the fixed
        # projection.  Exact-shape validation therefore fails closed instead
        # of claiming a lossless rebuild.
        raise RuntimeError(f"{table}_legacy_shape_invalid")
    if shadow_exists:
        # A table name alone cannot establish which side contains the canonical
        # rows.  Do not discard either side; require operator inspection.
        raise RuntimeError(f"{table}_rebuild_ambiguous_shadow")

    _inject_candidate_rebuild_fault(fault_injector, table, "before_rename")
    conn.execute(f'ALTER TABLE "{table}" RENAME TO "{shadow_table}"')
    _inject_candidate_rebuild_fault(fault_injector, table, "after_rename")

    _inject_candidate_rebuild_fault(fault_injector, table, "before_create")
    conn.execute(create_sql)
    _inject_candidate_rebuild_fault(fault_injector, table, "after_create")

    old_count = int(conn.execute(f'SELECT COUNT(*) FROM "{shadow_table}"').fetchone()[0])
    _inject_candidate_rebuild_fault(fault_injector, table, "before_copy")
    conn.execute(copy_sql)
    _inject_candidate_rebuild_fault(fault_injector, table, "after_copy")
    new_count = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    if new_count != old_count:
        raise RuntimeError(f"{table}_rebuild_count_mismatch")
    if conn.execute(f"SELECT 1 FROM ({old_projection} EXCEPT {new_projection}) LIMIT 1").fetchone():
        raise RuntimeError(f"{table}_rebuild_semantics_mismatch")
    if conn.execute(f"SELECT 1 FROM ({new_projection} EXCEPT {old_projection}) LIMIT 1").fetchone():
        raise RuntimeError(f"{table}_rebuild_semantics_mismatch")
    if _context_table_primary_key(conn, table) != current_primary_key:
        raise RuntimeError(f"{table}_rebuild_primary_key_mismatch")
    if not _context_unique_index_matches(conn, table, required_unique):
        raise RuntimeError(f"{table}_rebuild_unique_scope_mismatch")

    _inject_candidate_rebuild_fault(fault_injector, table, "before_drop")
    conn.execute(f'DROP TABLE "{shadow_table}"')
    _inject_candidate_rebuild_fault(fault_injector, table, "after_drop")
    if post_create_sql:
        conn.execute(post_create_sql)

def _inject_candidate_rebuild_fault(
    fault_injector: Callable[[str], None] | None,
    table: str,
    point: str,
) -> None:
    if fault_injector is not None:
        fault_injector(f"{table}.{point}")

def _context_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None

def _context_table_primary_key(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [
        str(row["name"] if hasattr(row, "keys") else row[1])
        for row in sorted(
            rows,
            key=lambda item: int(item["pk"] if hasattr(item, "keys") else item[5]),
        )
        if int(row["pk"] if hasattr(row, "keys") else row[5]) > 0
    ]

def _context_unique_index_matches(
    conn: sqlite3.Connection,
    table: str,
    expected_columns: tuple[str, ...],
) -> bool:
    for index in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
        unique = int(index["unique"] if hasattr(index, "keys") else index[2])
        if not unique:
            continue
        name = str(index["name"] if hasattr(index, "keys") else index[1])
        columns = tuple(
            str(row["name"] if hasattr(row, "keys") else row[2])
            for row in conn.execute(f'PRAGMA index_info("{name}")').fetchall()
        )
        if columns == expected_columns:
            return True
    return False

def _upgrade_context_v2_schema(
    conn: sqlite3.Connection,
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> None:
    """Atomically apply every cumulative schema-30 correction before Phase 2.

    A owns the exact four-column ingest scope and external binding contract;
    F owns the continuous transaction/savepoint barrier. E timezone validation,
    B delivery binding repair, and all additive migrations execute inside the
    same barrier. Any fault therefore leaves the pre-upgrade schema and rows
    byte-for-byte available for a clean retry.
    """
    started_transaction = not conn.in_transaction
    savepoint = "context_candidate_schema30_rebuild"
    if started_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        _upgrade_context_v2_schema_locked(
            conn,
            fault_injector=fault_injector,
        )
        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at "
            "WHERE settings.value<>excluded.value",
            ("context_candidate_table_rebuild_version", "3", _utc_now()),
        )
        if started_transaction:
            conn.commit()
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        if started_transaction:
            if conn.in_transaction:
                conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def _upgrade_context_v2_schema_locked(
    conn: sqlite3.Connection,
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> None:
    """Idempotently upgrade an earlier schema-30 candidate in place.

    The Group-A correction changes ingest idempotency from the unsafe v2
    principal-global scope to ``contract + stable principal + source + key``.
    The rebuild is transactional and preserves existing receipts.
    """
    # Validate first. An invalid configured or snapshotted IANA zone must not
    # cause a partial UTC rewrite of experiments or product-win boundaries.
    migration_timezone = _validated_context_v2_migration_timezone(conn)
    ingest_columns = _table_columns(conn, "context_ingest_receipts")
    expected_scope = ("contract_type", "principal_id", "source_ref", "idempotency_key")
    legacy_scope = ("contract_type", "principal_id", "idempotency_key")
    contracts = _index_contracts(conn, "context_ingest_receipts")
    expected_present = any(
        columns == expected_scope and unique and not partial
        for columns, unique, partial, _origin in contracts
    )
    legacy_present = any(
        columns == legacy_scope and unique
        for columns, unique, _partial, _origin in contracts
    )
    malformed_expected = any(
        columns == expected_scope and (not unique or partial)
        for columns, unique, partial, _origin in contracts
    )
    unexpected_unique = any(
        unique and columns not in {("receipt_ref",), expected_scope}
        for columns, unique, _partial, _origin in contracts
    )
    if ingest_columns and (
        not _ingest_receipt_column_contract_is_exact(conn)
        or not expected_present
        or legacy_present
        or malformed_expected
        or unexpected_unique
    ):
        _rebuild_context_ingest_receipts_scope(
            conn,
            fault_injector=fault_injector,
        )
    _upgrade_external_receipt_bindings(
        conn,
        fault_injector=fault_injector,
    )
    delivery_base_columns = {
        "receipt_ref",
        "delivery_ref",
        "principal_id",
        "key_id",
        "idempotency_key",
        "payload_hash",
        "transition",
        "occurred_at",
        "bundle_hash",
        "provider_receipt_ref",
        "failure_reason_code",
        "result_state",
        "created_at",
    }
    delivery_binding_columns = {
        "bundle_ref": "TEXT NOT NULL DEFAULT ''",
        "attempt_no": "INTEGER NOT NULL DEFAULT 0",
        "project_ref": "TEXT NOT NULL DEFAULT ''",
        "target_ref": "TEXT NOT NULL DEFAULT ''",
        "target_scope": "TEXT NOT NULL DEFAULT ''",
        "export_policy": "TEXT NOT NULL DEFAULT ''",
        "privacy_policy_hash": "TEXT NOT NULL DEFAULT ''",
        "principal_role": "TEXT NOT NULL DEFAULT ''",
    }
    delivery_columns = _table_columns(conn, "context_delivery_receipts")
    delivery_primary_key = _context_table_primary_key(
        conn, "context_delivery_receipts"
    )
    if delivery_primary_key == ["receipt_ref"] and delivery_columns == delivery_base_columns:
        for name, definition in delivery_binding_columns.items():
            _ensure_context_column(
                conn, "context_delivery_receipts", name, definition
            )
    _repair_candidate_receipt_table(
        conn,
        table="context_delivery_receipts",
        shadow_table="context_delivery_receipts_v1",
        legacy_primary_key=["idempotency_key"],
        current_primary_key=["receipt_ref"],
        legacy_columns=delivery_base_columns,
        current_columns=delivery_base_columns
        | {
            "bundle_ref",
            "attempt_no",
            "project_ref",
            "target_ref",
            "target_scope",
            "export_policy",
            "privacy_policy_hash",
            "principal_role",
        },
        create_sql="""
            CREATE TABLE context_delivery_receipts (
              receipt_ref TEXT PRIMARY KEY,
              delivery_ref TEXT NOT NULL,
              principal_id TEXT NOT NULL,
              key_id TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              payload_hash TEXT NOT NULL,
              bundle_ref TEXT NOT NULL DEFAULT '',
              attempt_no INTEGER NOT NULL DEFAULT 0,
              project_ref TEXT NOT NULL DEFAULT '',
              target_ref TEXT NOT NULL DEFAULT '',
              target_scope TEXT NOT NULL DEFAULT '',
              export_policy TEXT NOT NULL DEFAULT '',
              privacy_policy_hash TEXT NOT NULL DEFAULT '',
              principal_role TEXT NOT NULL DEFAULT '',
              transition TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              bundle_hash TEXT NOT NULL,
              provider_receipt_ref TEXT NOT NULL DEFAULT '',
              failure_reason_code TEXT NOT NULL DEFAULT '',
              result_state TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(delivery_ref) REFERENCES context_deliveries(delivery_ref),
              UNIQUE(delivery_ref, principal_id, idempotency_key)
            )
        """,
        copy_sql="""
            INSERT INTO context_delivery_receipts(
              receipt_ref,delivery_ref,principal_id,key_id,idempotency_key,payload_hash,
              transition,occurred_at,bundle_hash,provider_receipt_ref,failure_reason_code,
              result_state,created_at
            ) SELECT receipt_ref,delivery_ref,principal_id,key_id,idempotency_key,payload_hash,
              transition,occurred_at,bundle_hash,provider_receipt_ref,failure_reason_code,
              result_state,created_at FROM context_delivery_receipts_v1
        """,
        old_projection="""
            SELECT receipt_ref,delivery_ref,principal_id,key_id,idempotency_key,payload_hash,
              transition,occurred_at,bundle_hash,provider_receipt_ref,failure_reason_code,
              result_state,created_at FROM context_delivery_receipts_v1
        """,
        new_projection="""
            SELECT receipt_ref,delivery_ref,principal_id,key_id,idempotency_key,payload_hash,
              transition,occurred_at,bundle_hash,provider_receipt_ref,failure_reason_code,
              result_state,created_at FROM context_delivery_receipts
        """,
        required_unique=("delivery_ref", "principal_id", "idempotency_key"),
        post_create_sql="",
        fault_injector=fault_injector,
    )
    for table, columns in {
        "context_observations": {
            "source_event_ref": "TEXT NOT NULL DEFAULT ''",
            "principal_id": "TEXT NOT NULL DEFAULT ''",
            "key_id": "TEXT NOT NULL DEFAULT ''",
            "adapter_kind": "TEXT NOT NULL DEFAULT ''",
        },
        "context_principals": {
            "target_ref": "TEXT NOT NULL DEFAULT ''",
            "target_scope": "TEXT NOT NULL DEFAULT ''",
        },
        "context_evidence": {
            "project_ref": "TEXT NOT NULL DEFAULT ''",
            "workstream_ref": "TEXT NOT NULL DEFAULT ''",
            "object_ref": "TEXT NOT NULL DEFAULT ''",
            "version_ref": "TEXT NOT NULL DEFAULT ''",
            "subject_ref": "TEXT NOT NULL DEFAULT ''",
            "principal_id": "TEXT NOT NULL DEFAULT ''",
            "key_id": "TEXT NOT NULL DEFAULT ''",
            "adapter_kind": "TEXT NOT NULL DEFAULT ''",
        },
        "context_verified": {
            "verification_scope": "TEXT NOT NULL DEFAULT 'operational'",
            "memory_eligible": "INTEGER NOT NULL DEFAULT 0",
        },
        "context_target_policies": {
            "policy_declared": "INTEGER NOT NULL DEFAULT 0",
        },
        "context_bundle_manifests": {
            "stable_projection_json": "TEXT NOT NULL DEFAULT '{}'",
            "frozen_payload_json": "TEXT NOT NULL DEFAULT '{}'",
        },
        "context_deliveries": {
            "project_ref": "TEXT NOT NULL DEFAULT ''",
            "attempt_no": "INTEGER NOT NULL DEFAULT 1",
            "target_scope": "TEXT NOT NULL DEFAULT 'local'",
            "export_policy": "TEXT NOT NULL DEFAULT 'local_only'",
            "allowed_privacy_classes_json": "TEXT NOT NULL DEFAULT '[]'",
            "privacy_policy_hash": "TEXT NOT NULL DEFAULT ''",
        },
        "context_delivery_receipts": {
            "bundle_ref": "TEXT NOT NULL DEFAULT ''",
            "attempt_no": "INTEGER NOT NULL DEFAULT 0",
            "project_ref": "TEXT NOT NULL DEFAULT ''",
            "target_ref": "TEXT NOT NULL DEFAULT ''",
            "target_scope": "TEXT NOT NULL DEFAULT ''",
            "export_policy": "TEXT NOT NULL DEFAULT ''",
            "privacy_policy_hash": "TEXT NOT NULL DEFAULT ''",
            "principal_role": "TEXT NOT NULL DEFAULT ''",
        },
        "context_candidate_evidence": {
            "dependency_role": "TEXT NOT NULL DEFAULT 'required'",
        },
        "context_product_experiments": {
            "timezone_name": "TEXT NOT NULL DEFAULT ''",
        },
        "context_product_experiment_attempts": {
            "event_id": "TEXT NOT NULL DEFAULT ''",
            "useful": "INTEGER",
        },
        "context_product_wins": {
            "local_date": "TEXT NOT NULL DEFAULT ''",
            "expires_local_date": "TEXT NOT NULL DEFAULT ''",
            "timezone_name": "TEXT NOT NULL DEFAULT ''",
        },
    }.items():
        for name, definition in columns.items():
            _ensure_context_column(conn, table, name, definition)
    # Earlier v2 candidates did not snapshot project identity and gave every
    # existing delivery the additive default attempt 1.  Recover only the
    # manifest-bound project and a deterministic per-project/target sequence;
    # all old receipts keep empty binding columns and therefore remain
    # ineligible for replay until a new fully bound attempt is prepared.
    conn.execute(
        "UPDATE context_deliveries SET project_ref=(SELECT m.project_ref FROM context_bundle_manifests m "
        "WHERE m.bundle_ref=context_deliveries.bundle_ref) WHERE project_ref='' AND EXISTS "
        "(SELECT 1 FROM context_bundle_manifests m WHERE m.bundle_ref=context_deliveries.bundle_ref "
        "AND m.project_ref<>'')"
    )
    project_attempt_index = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_context_delivery_project_attempt'"
    ).fetchone()
    if not project_attempt_index:
        counters: dict[tuple[str, str], int] = {}
        legacy_deliveries = conn.execute(
            "SELECT delivery_ref,project_ref,target_ref,attempt_no FROM context_deliveries "
            "ORDER BY project_ref,target_ref,datetime(created_at),delivery_ref"
        ).fetchall()
        for delivery in legacy_deliveries:
            project_ref = str(delivery["project_ref"] or "")
            target_ref = str(delivery["target_ref"] or "")
            if not project_ref or not target_ref:
                continue
            key = (project_ref, target_ref)
            expected_attempt = counters.get(key, 0) + 1
            counters[key] = expected_attempt
            if int(delivery["attempt_no"] or 0) != expected_attempt:
                conn.execute(
                    "UPDATE context_deliveries SET attempt_no=? WHERE delivery_ref=?",
                    (expected_attempt, str(delivery["delivery_ref"])),
                )
    upgrade_stamp = _utc_now()
    conn.execute(
        "UPDATE context_deliveries SET state='failed',"
        "failed_at=COALESCE(failed_at,NULLIF(updated_at,''),NULLIF(created_at,''),?),"
        "failure_reason_code='delivery_binding_upgrade_required',updated_at=? "
        "WHERE state IN ('prepared','consumed') AND "
        "(project_ref='' OR target_scope NOT IN ('local','external') OR "
        "export_policy NOT IN ('local_only','explicit_export') OR "
        "allowed_privacy_classes_json IN ('','[]') OR privacy_policy_hash='')",
        (upgrade_stamp, upgrade_stamp),
    )
    conn.execute(
        "UPDATE context_product_experiment_attempts SET event_id=source_ref "
        "WHERE event_id=''"
    )
    conn.execute(
        "UPDATE context_product_experiment_attempts SET useful="
        "CASE outcome WHEN 'useful' THEN 1 WHEN 'not_useful' THEN 0 ELSE NULL END "
        "WHERE useful IS NULL AND outcome IN ('useful','not_useful')"
    )
    conn.execute(
        "UPDATE context_product_experiments SET timezone_name=? WHERE timezone_name=''",
        (migration_timezone,),
    )
    conn.execute(
        "UPDATE context_product_experiments SET minimum_days=?,maximum_days=? "
        "WHERE minimum_days<>? OR maximum_days<>?",
        (
            PRODUCT_EXPERIMENT_MIN_DAYS,
            PRODUCT_EXPERIMENT_MAX_DAYS,
            PRODUCT_EXPERIMENT_MIN_DAYS,
            PRODUCT_EXPERIMENT_MAX_DAYS,
        ),
    )
    legacy_wins = conn.execute(
        "SELECT win_ref,occurred_at FROM context_product_wins "
        "WHERE local_date='' OR expires_local_date='' OR timezone_name=''"
    ).fetchall()
    for legacy_win in legacy_wins:
        try:
            occurred = _parse_contract_time(str(legacy_win["occurred_at"]))
            local_date, expires_local_date, expires_at, timezone_name = _local_retention_window(
                conn,
                occurred,
                PRODUCT_WIN_RETENTION_DAYS,
                timezone_name=migration_timezone,
            )
        except (ContractError, TypeError, ValueError):
            continue
        conn.execute(
            "UPDATE context_product_wins SET local_date=?,expires_local_date=?,timezone_name=?,expires_at=? "
            "WHERE win_ref=?",
            (local_date, expires_local_date, timezone_name, expires_at, str(legacy_win["win_ref"])),
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_context_experiment_event "
        "ON context_product_experiment_attempts(experiment_ref,event_id)"
    )
    duplicate_running_experiment = conn.execute(
        "SELECT 1 FROM context_product_experiments WHERE status='running' "
        "GROUP BY status HAVING COUNT(*)>1 LIMIT 1"
    ).fetchone()
    if duplicate_running_experiment:
        raise sqlite3.DatabaseError("multiple_running_product_experiments")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_context_single_running_experiment "
        "ON context_product_experiments(status) WHERE status='running'"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_context_delivery_attempt "
        "ON context_deliveries(bundle_ref,target_ref,attempt_no)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_context_delivery_project_attempt "
        "ON context_deliveries(project_ref,target_ref,attempt_no) WHERE project_ref<>''"
    )


def configure_context_console(
    conn: sqlite3.Connection,
    *,
    enabled: bool,
    confirmation: str = "",
) -> dict[str, Any]:
    if enabled and confirmation != CONTEXT_FEATURE_CONFIRMATION:
        return _result("rejected", "explicit_enable_confirmation_required")
    try:
        # Feature-state transitions share the same cross-process ordering
        # primitive as trusted reads and handoff persistence. A read already
        # holding a shared lease linearizes before disable; a read that arrives
        # later must observe the committed disabled state and fail closed.
        with _context_authorization_lock(conn, exclusive=True):
            conn.execute("BEGIN IMMEDIATE")
            now = _utc_now()
            conn.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (CONTEXT_FEATURE_FLAG, "true" if enabled else "false", now),
            )
            conn.commit()
    except (sqlite3.Error, RuntimeError):
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "feature_flag_write_failed")
    except BaseException:
        # This function owns the transaction it opens.  Process-control
        # exceptions must not leave a pending feature transition that a later,
        # unrelated write could accidentally commit.
        if conn.in_transaction:
            conn.rollback()
        raise
    return {
        "status": "enabled" if enabled else "disabled",
        "feature_flag": CONTEXT_FEATURE_FLAG,
        "raw_access": False,
        "memory_writer_enabled": False,
        "source_revocation_cascade_ready": False,
    }


def context_console_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (CONTEXT_FEATURE_FLAG,)).fetchone()
    return bool(row and str(row["value"] if hasattr(row, "keys") else row[0]).lower() == "true")


def _active_document_mapping_locked(
    conn: sqlite3.Connection,
    *,
    root_id: str,
    occurred_at: datetime,
) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM context_document_mappings WHERE root_id=? AND status='active' "
        "ORDER BY mapping_version DESC LIMIT 1",
        (root_id,),
    ).fetchone()
    if not row:
        return None
    effective_at = _parse_contract_time(str(row["effective_at"]))
    expires_at = _parse_optional_time(str(row["expires_at"] or ""))
    if occurred_at < effective_at:
        return None
    if expires_at is not None and occurred_at >= expires_at:
        return None
    return row


def _register_document_context_mapping_locked(
    conn: sqlite3.Connection,
    *,
    mapping_ref: str,
    mapping_version: int,
    root_id: str,
    project_ref: str,
    workstream_ref: str,
    object_ref: str,
    version_ref: str,
    subject_ref: str,
    effective_at: str | None = None,
    expires_at: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _aware_utc(now)
    stamp = _iso(current)
    try:
        _validate_opaque_ref("mapping_ref", mapping_ref)
        version_number = int(mapping_version)
        if version_number < 1:
            raise ContractError("invalid_mapping_version")
        if not isinstance(root_id, str) or not re.fullmatch(r"root_[0-9a-f]{32}", root_id):
            raise ContractError("invalid_document_root")
        effective = _parse_contract_time(effective_at or stamp)
        expiry = _parse_optional_time(expires_at)
        if expiry is not None and expiry <= effective:
            raise ContractError("invalid_mapping_expiry")
        refs = {
            "project_ref": project_ref,
            "workstream_ref": workstream_ref,
            "object_ref": object_ref,
            "version_ref": version_ref,
            "subject_ref": subject_ref,
        }
        _validate_registry_refs(conn, refs)
    except (ContractError, TypeError, ValueError) as exc:
        reason = exc.reason_code if isinstance(exc, ContractError) else "invalid_mapping_version"
        return _result("rejected", reason)

    try:
        conn.execute("BEGIN IMMEDIATE")
        root = conn.execute(
            "SELECT enabled FROM file_watch_roots WHERE root_id=?",
            (root_id,),
        ).fetchone()
        if not root or not bool(root["enabled"]):
            raise ContractError("document_root_not_authorized")
        _validate_registry_refs(conn, refs)
        existing = conn.execute(
            "SELECT * FROM context_document_mappings WHERE mapping_ref=? AND mapping_version=?",
            (mapping_ref, version_number),
        ).fetchone()
        principal_id = _derived_ref("principal", f"document-mapping|{mapping_ref}|{version_number}")
        key_id = _derived_ref("key", f"document-mapping-key|{mapping_ref}|{version_number}")
        fingerprint = _hash_text(f"internal-document-mapping|{mapping_ref}|{version_number}|{project_ref}")
        expected = {
            "root_id": root_id,
            "project_ref": project_ref,
            "workstream_ref": workstream_ref,
            "object_ref": object_ref,
            "version_ref": version_ref,
            "subject_ref": subject_ref,
            "principal_id": principal_id,
            "key_id": key_id,
            "effective_at": _iso(effective),
            "expires_at": _iso(expiry) if expiry is not None else "",
        }
        if existing:
            exact = all(str(existing[key] or "") == str(value) for key, value in expected.items())
            if not exact:
                conn.rollback()
                return _result("conflict", "document_mapping_conflict")
            if str(existing["status"] or "") != "active":
                conn.rollback()
                return _result("rejected", "document_mapping_not_active")
            principal = conn.execute(
                "SELECT status,principal_kind,project_ref,key_fingerprint FROM context_principals "
                "WHERE principal_id=? AND key_id=?",
                (principal_id, key_id),
            ).fetchone()
            if not principal or (
                str(principal["status"] or "") != "active"
                or str(principal["principal_kind"] or "")
                not in {"file_adapter", DOCUMENT_MAPPING_PRINCIPAL_KIND}
                or str(principal["project_ref"] or "") != project_ref
                or str(principal["key_fingerprint"] or "") != fingerprint
            ):
                raise ContractError("document_mapping_principal_invalid")
            conn.commit()
            return {
                "status": "replayed",
                "reason_code": "idempotent_replay",
                "contract_version": DOCUMENT_MAPPING_CONTRACT_VERSION,
                "mapping_ref": mapping_ref,
                "mapping_version": version_number,
                "root_id": root_id,
                "principal_id": principal_id,
                "key_id": key_id,
                "raw_access": False,
            }
        latest = conn.execute(
            "SELECT COALESCE(MAX(mapping_version),0) FROM context_document_mappings WHERE mapping_ref=?",
            (mapping_ref,),
        ).fetchone()[0]
        if int(latest or 0) >= version_number:
            raise ContractError("mapping_version_not_monotonic")
        superseded_principals = conn.execute(
            "SELECT DISTINCT principal_id,key_id FROM context_document_mappings "
            "WHERE root_id=? ORDER BY principal_id,key_id",
            (root_id,),
        ).fetchall()
        superseded = conn.execute(
            "UPDATE context_document_mappings SET status='superseded',revoked_at=?,"
            "revocation_reason_code='mapping_version_superseded' WHERE root_id=? AND status='active'",
            (stamp, root_id),
        ).rowcount
        supersede_cascade = _zero_cascade()
        superseded_principal_count = 0
        for old_principal in superseded_principals:
            principal_result = _revoke_context_principal_rows_locked(
                conn,
                str(old_principal["principal_id"] or ""),
                str(old_principal["key_id"] or ""),
                reason_code="mapping_version_superseded",
                now=current,
                preserve_document_history=True,
            )
            superseded_principal_count += int(principal_result["principal_revoked"])
            _merge_cascade(supersede_cascade, principal_result["cascade"])
        principal = conn.execute(
            "SELECT status,principal_kind,project_ref,key_fingerprint FROM context_principals "
            "WHERE principal_id=? AND key_id=?",
            (principal_id, key_id),
        ).fetchone()
        if principal:
            if not (
                str(principal["status"] or "") == "active"
                and str(principal["principal_kind"] or "") == DOCUMENT_MAPPING_PRINCIPAL_KIND
                and str(principal["project_ref"] or "") == project_ref
                and str(principal["key_fingerprint"] or "") == fingerprint
            ):
                raise ContractError("document_mapping_principal_invalid")
        else:
            conn.execute(
                "INSERT INTO context_principals(principal_id,key_id,principal_kind,project_ref,key_fingerprint,"
                "status,created_at,revoked_at) VALUES(?,?,?,?,?,'active',?,NULL)",
                (
                    principal_id,
                    key_id,
                    DOCUMENT_MAPPING_PRINCIPAL_KIND,
                    project_ref,
                    fingerprint,
                    stamp,
                ),
            )
        conn.execute(
            "INSERT INTO context_document_mappings(mapping_ref,mapping_version,contract_version,root_id,project_ref,"
            "workstream_ref,object_ref,version_ref,subject_ref,principal_id,key_id,status,effective_at,expires_at,"
            "created_at,revoked_at,revocation_reason_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,NULL,'')",
            (
                mapping_ref,
                version_number,
                DOCUMENT_MAPPING_CONTRACT_VERSION,
                root_id,
                project_ref,
                workstream_ref,
                object_ref,
                version_ref,
                subject_ref,
                principal_id,
                key_id,
                _iso(effective),
                _iso(expiry) if expiry is not None else "",
                stamp,
            ),
        )
        conn.commit()
        return {
            "status": "registered",
            "contract_version": DOCUMENT_MAPPING_CONTRACT_VERSION,
            "mapping_ref": mapping_ref,
            "mapping_version": version_number,
            "root_id": root_id,
            "principal_id": principal_id,
            "key_id": key_id,
            "superseded_mapping_count": int(superseded or 0),
            "superseded_principal_count": superseded_principal_count,
            "supersede_cascade": supersede_cascade,
            "raw_access": False,
        }
    except ContractError as exc:
        if conn.in_transaction:
            conn.rollback()
        return _result("rejected", exc.reason_code)
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "document_mapping_write_failed")


def register_document_context_mapping(conn: sqlite3.Connection, **kwargs: Any) -> dict[str, Any]:
    with _context_authorization_lock(conn, exclusive=True):
        return _register_document_context_mapping_locked(conn, **kwargs)


def _register_context_label_locked(
    conn: sqlite3.Connection,
    *,
    kind: str,
    ref: str,
    label: str,
    project_ref: str = "",
    subject_scope: str = "",
    target_scope: str | None = None,
    allowed_privacy_classes: Sequence[str] | None = None,
    export_policy: str | None = None,
) -> dict[str, Any]:
    if kind not in LABEL_KINDS:
        return _result("rejected", "invalid_label_kind")
    try:
        _validate_ref_for_kind(kind, ref)
        clean_label = _validate_approved_label(label)
        if kind == "project":
            project_ref = ref
        else:
            _validate_opaque_ref("project_ref", project_ref)
        if kind == "subject":
            if subject_scope not in SUBJECT_SCOPES:
                raise ContractError("invalid_subject_scope")
        elif subject_scope:
            raise ContractError("subject_scope_not_allowed")
        if kind == "target":
            if target_scope is None or export_policy is None or allowed_privacy_classes is None:
                raise ContractError("target_policy_required")
            if target_scope not in TARGET_SCOPES:
                raise ContractError("invalid_target_scope")
            if export_policy not in TARGET_EXPORT_POLICIES:
                raise ContractError("invalid_target_export_policy")
            allowed = sorted(set(allowed_privacy_classes))
            if not allowed or any(value not in PRIVACY_CLASSES for value in allowed):
                raise ContractError("invalid_target_privacy_policy")
            if target_scope == "local" and export_policy != "local_only":
                raise ContractError("local_target_requires_local_only")
            if target_scope == "external" and export_policy != "explicit_export":
                raise ContractError("external_target_requires_explicit_export")
            if target_scope == "external" and "restricted_local" in allowed:
                raise ContractError("restricted_local_export_forbidden")
        else:
            if target_scope is not None or export_policy is not None or allowed_privacy_classes is not None:
                raise ContractError("target_policy_not_allowed")
            allowed = []
    except ContractError as exc:
        return _result("rejected", exc.reason_code)
    now = _utc_now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if kind != "project":
            project = _label_row(conn, project_ref, "project")
            if not project:
                raise ContractError("project_unregistered")
        existing = conn.execute(
            "SELECT kind,label,project_ref,subject_scope,status FROM context_label_registry WHERE ref=?",
            (ref,),
        ).fetchone()
        if existing:
            exact = (
                str(existing["kind"]) == kind
                and str(existing["label"]) == clean_label
                and str(existing["project_ref"] or "") == project_ref
                and str(existing["subject_scope"] or "") == subject_scope
            )
            if str(existing["status"] or "") != "active":
                raise ContractError("label_revoked")
            if not exact:
                conn.rollback()
                return _result("conflict", "label_registration_conflict")
            if kind == "target":
                policy = conn.execute(
                    "SELECT target_scope,allowed_privacy_classes,export_policy,policy_declared "
                    "FROM context_target_policies WHERE target_ref=?",
                    (ref,),
                ).fetchone()
                if not policy:
                    conn.execute(
                        "INSERT INTO context_target_policies(target_ref,target_scope,allowed_privacy_classes,"
                        "export_policy,policy_declared,updated_at) VALUES(?,?,?,?,1,?)",
                        (ref, target_scope, json.dumps(allowed, separators=(",", ":")), export_policy, now),
                    )
                    conn.commit()
                    return {
                        "status": "registered",
                        "reason_code": "target_policy_declared",
                        "kind": kind,
                        "ref": ref,
                        "target_policy": {
                            "target_scope": target_scope,
                            "allowed_privacy_classes": allowed,
                            "export_policy": export_policy,
                        },
                        "raw_access": False,
                    }
                try:
                    stored_allowed = json.loads(str(policy["allowed_privacy_classes"] or "[]"))
                except (TypeError, json.JSONDecodeError):
                    conn.rollback()
                    return _result("conflict", "target_policy_conflict")
                policy_values_match = bool(
                    isinstance(stored_allowed, list)
                    and stored_allowed == allowed
                    and str(policy["target_scope"] or "") == target_scope
                    and str(policy["export_policy"] or "") == export_policy
                )
                if not policy_values_match:
                    conn.rollback()
                    return _result("conflict", "target_policy_conflict")
                if int(policy["policy_declared"] or 0) != 1:
                    conn.execute(
                        "UPDATE context_target_policies SET policy_declared=1,updated_at=? WHERE target_ref=?",
                        (now, ref),
                    )
                    conn.commit()
                    return {
                        "status": "registered",
                        "reason_code": "target_policy_declared",
                        "kind": kind,
                        "ref": ref,
                        "target_policy": {
                            "target_scope": target_scope,
                            "allowed_privacy_classes": allowed,
                            "export_policy": export_policy,
                        },
                        "raw_access": False,
                    }
            conn.commit()
            result = {
                "status": "replayed",
                "reason_code": "idempotent_replay",
                "kind": kind,
                "ref": ref,
                "raw_access": False,
            }
            if kind == "target":
                result["target_policy"] = {
                    "target_scope": target_scope,
                    "allowed_privacy_classes": allowed,
                    "export_policy": export_policy,
                }
            return result
        conn.execute(
            "INSERT INTO context_label_registry(ref,kind,label,project_ref,subject_scope,status,created_at,revoked_at) "
            "VALUES(?,?,?,?,?,'active',?,NULL)",
            (ref, kind, clean_label, project_ref, subject_scope, now),
        )
        if kind == "target":
            conn.execute(
                "INSERT INTO context_target_policies(target_ref,target_scope,allowed_privacy_classes,export_policy,"
                "policy_declared,updated_at) VALUES(?,?,?,?,1,?)",
                (ref, target_scope, json.dumps(allowed, separators=(",", ":")), export_policy, now),
            )
        conn.commit()
        result = {"status": "registered", "kind": kind, "ref": ref, "raw_access": False}
        if kind == "target":
            result["target_policy"] = {
                "target_scope": target_scope,
                "allowed_privacy_classes": allowed,
                "export_policy": export_policy,
            }
        return result
    except ContractError as exc:
        if conn.in_transaction:
            conn.rollback()
        return _result("rejected", exc.reason_code)
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "database_write_failed")

def _revoke_context_label_locked(conn: sqlite3.Connection, ref: str) -> dict[str, Any]:
    stamp = _utc_now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT kind,status FROM context_label_registry WHERE ref=?", (ref,)
        ).fetchone()
        if not row or str(row["status"] or "") != "active":
            conn.rollback()
            return _result("not_found", "label_not_active")
        kind = str(row["kind"] or "")
        column = {
            "project": "project_ref",
            "workstream": "workstream_ref",
            "object": "object_ref",
            "version": "version_ref",
            "subject": "subject_ref",
        }.get(kind)
        observations = []
        mapping_principals = []
        if column:
            observations = conn.execute(
                f"SELECT observation_ref FROM context_observations WHERE {column}=? AND status='active'",
                (ref,),
            ).fetchall()
            mapping_principals = conn.execute(
                f"SELECT DISTINCT principal_id,key_id FROM context_document_mappings "
                f"WHERE {column}=? ORDER BY principal_id,key_id",
                (ref,),
            ).fetchall()
        cursor = conn.execute(
            "UPDATE context_label_registry SET status='revoked',revoked_at=? WHERE ref=? AND status='active'",
            (stamp, ref),
        )
        if int(cursor.rowcount or 0) != 1:
            raise ContractError("label_not_active")
        mappings_revoked = 0
        if column:
            mapping_cursor = conn.execute(
                f"UPDATE context_document_mappings SET status='revoked',revoked_at=?,"
                f"revocation_reason_code=? WHERE {column}=? AND status='active'",
                (stamp, f"{kind}_label_revoked", ref),
            )
            mappings_revoked = int(mapping_cursor.rowcount or 0)
        total = _zero_cascade()
        current = _parse_contract_time(stamp)
        mapping_principals_revoked = 0
        for mapping_principal in mapping_principals:
            principal_result = _revoke_context_principal_rows_locked(
                conn,
                str(mapping_principal["principal_id"] or ""),
                str(mapping_principal["key_id"] or ""),
                reason_code=f"{kind}_label_revoked",
                now=current,
            )
            mapping_principals_revoked += int(principal_result["principal_revoked"])
            _merge_cascade(total, principal_result["cascade"])
        for observation in observations:
            _merge_cascade(
                total,
                _cascade_observation_locked(
                    conn,
                    str(observation["observation_ref"]),
                    terminal_status="revoked",
                    reason_code=f"{kind}_label_revoked",
                    now=current,
                ),
            )
        if kind == "target":
            delivery_rows = conn.execute(
                "SELECT delivery_ref,bundle_ref,bundle_hash,state FROM context_deliveries WHERE target_ref=?",
                (ref,),
            ).fetchall()
            _merge_cascade(
                total,
                _restrict_deliveries_for_dependency_locked(
                    conn,
                    delivery_rows=delivery_rows,
                    reason_code="target_label_revoked",
                    stamp=stamp,
                ),
            )
        conn.commit()
        return {
            "status": "revoked",
            "ref": ref,
            "kind": kind,
            "dependent_observation_count": len(observations),
            "document_mappings_revoked": mappings_revoked,
            "document_mapping_principals_revoked": mapping_principals_revoked,
            "cascade": total,
            "raw_access": False,
        }
    except ContractError as exc:
        if conn.in_transaction:
            conn.rollback()
        return _result("rejected", exc.reason_code)
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "label_revocation_failed")

def _register_context_principal_locked(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    key_id: str,
    principal_kind: str,
    verification_secret: bytes | str,
    project_ref: str = "",
    target_ref: str = "",
    target_scope: str = "",
) -> dict[str, Any]:
    try:
        _validate_opaque_ref("principal_id", principal_id)
        _validate_opaque_ref("key_id", key_id)
        if principal_kind not in PRINCIPAL_KINDS:
            raise ContractError("invalid_principal_kind")
        secret = _secret_bytes(verification_secret)
        if len(secret) < 16:
            raise ContractError("verification_secret_too_short")
        delivery_principal = principal_kind in {"bundle_consumer", "delivery_provider"}
        if delivery_principal:
            if not project_ref:
                raise ContractError("delivery_principal_project_required")
            _validate_opaque_ref("target_ref", target_ref)
            if target_scope not in TARGET_SCOPES:
                raise ContractError("delivery_principal_scope_required")
        elif target_ref or target_scope:
            raise ContractError("delivery_scope_not_allowed")
        if project_ref:
            _require_active_label(conn, project_ref, "project")
        if delivery_principal:
            target = _require_active_label(conn, target_ref, "target")
            if str(target["project_ref"] or "") != project_ref:
                raise ContractError("principal_target_project_mismatch")
            policy = _load_target_policy(conn, target_ref, project_ref=project_ref)
            if str(policy["target_scope"]) != target_scope:
                raise ContractError("principal_target_scope_mismatch")
    except ContractError as exc:
        return _result("rejected", exc.reason_code)
    fingerprint = _secret_fingerprint(secret)
    now = _utc_now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT principal_kind,project_ref,target_ref,target_scope,key_fingerprint,status FROM context_principals "
            "WHERE principal_id=? AND key_id=?",
            (principal_id, key_id),
        ).fetchone()
        if existing:
            if str(existing["status"] or "") != "active":
                conn.rollback()
                return _result("rejected", "key_revoked")
            kind_and_key_match = (
                str(existing["principal_kind"]) == principal_kind
                and hmac.compare_digest(str(existing["key_fingerprint"]), fingerprint)
            )
            exact = (
                kind_and_key_match
                and str(existing["project_ref"] or "") == project_ref
                and str(existing["target_ref"] or "") == target_ref
                and str(existing["target_scope"] or "") == target_scope
            )
            if exact:
                conn.commit()
                return {
                    "status": "replayed",
                    "reason_code": "idempotent_replay",
                    "principal_id": principal_id,
                    "key_id": key_id,
                    "principal_kind": principal_kind,
                    "project_ref": project_ref,
                    "target_ref": target_ref,
                    "target_scope": target_scope,
                    "raw_access": False,
                }
            legacy_unscoped_delivery_principal = bool(
                delivery_principal
                and kind_and_key_match
                and str(existing["target_ref"] or "") == ""
                and str(existing["target_scope"] or "") == ""
                and str(existing["project_ref"] or "") in {"", project_ref}
            )
            if legacy_unscoped_delivery_principal:
                conn.execute(
                    "UPDATE context_principals SET project_ref=?,target_ref=?,target_scope=? "
                    "WHERE principal_id=? AND key_id=? AND status='active'",
                    (project_ref, target_ref, target_scope, principal_id, key_id),
                )
                conn.commit()
                return {
                    "status": "registered",
                    "reason_code": "delivery_scope_bound",
                    "principal_id": principal_id,
                    "key_id": key_id,
                    "principal_kind": principal_kind,
                    "project_ref": project_ref,
                    "target_ref": target_ref,
                    "target_scope": target_scope,
                    "raw_access": False,
                }
            conn.rollback()
            return _result("conflict", "principal_registration_conflict")
        conn.execute(
            "INSERT INTO context_principals(principal_id,key_id,principal_kind,project_ref,target_ref,target_scope,key_fingerprint,status,created_at,revoked_at) "
            "VALUES(?,?,?,?,?,?,?,'active',?,NULL)",
            (principal_id, key_id, principal_kind, project_ref, target_ref, target_scope, fingerprint, now),
        )
        conn.commit()
        return {
            "status": "registered",
            "principal_id": principal_id,
            "key_id": key_id,
            "principal_kind": principal_kind,
            "target_ref": target_ref,
            "target_scope": target_scope,
            "raw_access": False,
        }
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "database_write_failed")


def _revoke_context_principal_rows_locked(
    conn: sqlite3.Connection,
    principal_id: str,
    key_id: str,
    *,
    reason_code: str,
    now: datetime,
    preserve_document_history: bool = False,
) -> dict[str, Any]:
    """Revoke one principal and all locatable dependants in the caller's transaction."""
    stamp = _iso(now)
    document_history_filter = (
        "AND NOT EXISTS (SELECT 1 FROM context_document_source_versions dsv "
        "WHERE dsv.observation_ref=o.observation_ref) "
        if preserve_document_history
        else ""
    )
    observation_rows = conn.execute(
        "SELECT DISTINCT o.observation_ref FROM context_observations o "
        "LEFT JOIN context_evidence e ON e.observation_ref=o.observation_ref "
        "WHERE o.status='active' AND ((o.principal_id=? AND o.key_id=?) "
        "OR (e.principal_id=? AND e.key_id=?)) "
        f"{document_history_filter}ORDER BY o.observation_ref",
        (principal_id, key_id, principal_id, key_id),
    ).fetchall()
    cursor = conn.execute(
        "UPDATE context_principals SET status='revoked',revoked_at=? "
        "WHERE principal_id=? AND key_id=? AND status='active'",
        (stamp, principal_id, key_id),
    )
    total = _zero_cascade()
    for observation in observation_rows:
        _merge_cascade(
            total,
            _cascade_observation_locked(
                conn,
                str(observation["observation_ref"]),
                terminal_status="revoked",
                reason_code=reason_code,
                now=now,
            ),
        )
    delivery_rows = conn.execute(
        "SELECT DISTINCT d.delivery_ref,d.bundle_ref,d.bundle_hash,d.state "
        "FROM context_deliveries d JOIN context_delivery_receipts r ON r.delivery_ref=d.delivery_ref "
        "WHERE r.principal_id=? AND r.key_id=?",
        (principal_id, key_id),
    ).fetchall()
    _merge_cascade(
        total,
        _restrict_deliveries_for_dependency_locked(
            conn,
            delivery_rows=delivery_rows,
            reason_code=reason_code,
            stamp=stamp,
        ),
    )
    return {
        "principal_revoked": int(cursor.rowcount or 0),
        "dependent_observation_count": len(observation_rows),
        "cascade": total,
    }


def _revoke_context_principal_locked(conn: sqlite3.Connection, principal_id: str, key_id: str) -> dict[str, Any]:
    current = _aware_utc(None)
    try:
        _validate_opaque_ref("principal_id", principal_id)
        _validate_opaque_ref("key_id", key_id)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM context_principals WHERE principal_id=? AND key_id=?",
            (principal_id, key_id),
        ).fetchone()
        if not row or str(row["status"] or "") != "active":
            conn.rollback()
            return _result("not_found", "principal_not_active")
        revoked = _revoke_context_principal_rows_locked(
            conn,
            principal_id,
            key_id,
            reason_code="principal_key_revoked",
            now=current,
        )
        if int(revoked["principal_revoked"]) != 1:
            raise ContractError("principal_not_active")
        conn.commit()
        return {
            "status": "revoked",
            "principal_id": principal_id,
            "key_id": key_id,
            "dependent_observation_count": int(revoked["dependent_observation_count"]),
            "cascade": revoked["cascade"],
            "raw_access": False,
        }
    except ContractError as exc:
        if conn.in_transaction:
            conn.rollback()
        return _result("rejected", exc.reason_code)
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "principal_revocation_failed")

def sign_contract_payload(payload: Mapping[str, Any], verification_secret: bytes | str) -> str:
    """Sign a normalized contract without placing the key or body in argv.

    Callers should pass the contract through stdin/private IPC and obtain the key
    from an equally private local channel. This helper exists for controlled
    adapters and tests; it never stores the secret.
    """
    body = dict(payload)
    body.pop("signature", None)
    normalized = _normalize_for_signature(body)
    digest = hmac.new(_secret_bytes(verification_secret), _canonical_bytes(normalized), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def _ingest_context_receipt_locked(
    conn: sqlite3.Connection,
    raw_contract: bytes | str,
    *,
    verification_secret: bytes | str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not context_console_enabled(conn):
        return _result("disabled", "feature_flag_off")
    try:
        payload = _parse_closed_json(raw_contract, _CONTEXT_RECEIPT_FIELDS)
        normalized = _validate_context_receipt(conn, payload)
        _authenticate_principal(
            conn,
            principal_id=str(normalized["producer_id"]),
            key_id=str(normalized["key_id"]),
            verification_secret=verification_secret,
            expected_kinds={"ai_lifecycle"},
            project_ref=str(normalized["project_ref"]),
        )
        _verify_signature(normalized, verification_secret)
    except ContractError as exc:
        return _result("rejected", exc.reason_code)
    payload_hash = _payload_hash(normalized)
    current = _aware_utc(now)
    principal_id = str(normalized["producer_id"])
    source_ref = str(normalized["source_ref"])
    try:
        conn.execute("BEGIN IMMEDIATE")
        normalized = _validate_context_receipt(conn, payload)
        principal = _authenticate_principal(
            conn,
            principal_id=principal_id,
            key_id=str(normalized["key_id"]),
            verification_secret=verification_secret,
            expected_kinds={"ai_lifecycle"},
            project_ref=str(normalized["project_ref"]),
        )
        _verify_signature(normalized, verification_secret)

        # Delete/revoke wins before either semantic or idempotent replay.
        source_event_ref = _canonical_source_event_ref(
            CONTEXT_RECEIPT_VERSION, principal.principal_id, normalized
        )
        semantic_hash = _source_event_semantic_hash(CONTEXT_RECEIPT_VERSION, normalized)
        source_replay = _source_event_guard_locked(
            conn, source_event_ref=source_event_ref, semantic_hash=semantic_hash
        )
        replay = _scoped_ingest_idempotency_locked(
            conn,
            contract_type=CONTEXT_RECEIPT_VERSION,
            principal_id=principal.principal_id,
            source_ref=str(normalized["source_ref"]),
            idempotency_key=str(normalized["idempotency_key"]),
            payload_hash=payload_hash,
        )
        if replay is not None:
            conn.commit()
            return replay
        if source_replay and source_replay.get("semantic_match") != "1":
            raise ContractError("canonical_source_event_conflict")

        # A different idempotency key is a new network submission even when
        # it names an existing canonical source.  Recheck its signed
        # timestamp before returning a semantic replay, and never persist a
        # second receipt alias for the same source event.
        occurred = _parse_contract_time(str(normalized["occurred_at"]))
        _validate_freshness(occurred, current)
        receipt_ref = _derived_ref(
            "receipt",
            f"{CONTEXT_RECEIPT_VERSION}|{principal.principal_id}|{normalized['source_ref']}|"
            f"{normalized['idempotency_key']}|{payload_hash}",
        )
        if source_replay:
            conn.commit()
            return {
                "status": "replayed",
                "reason_code": "canonical_source_event_replay",
                "receipt_ref": source_replay["receipt_ref"],
                "observation_ref": source_replay["observation_ref"],
                "result_status": source_replay["result_status"],
                "source_event_ref": source_event_ref,
                "raw_access": False,
            }

        _validate_evidence_refs(
            conn,
            normalized["evidence_refs"],
            str(normalized["project_ref"]),
            current,
            workstream_ref=str(normalized["workstream_ref"]),
            object_ref=str(normalized["object_ref"]),
            version_ref=str(normalized["version_ref"]),
            subject_ref=str(normalized["subject_ref"]),
            privacy_class=str(normalized["privacy_class"]),
            occurred_at=occurred,
        )
        refs = _materialize_ai_receipt(
            conn,
            normalized,
            payload_hash,
            current,
            principal,
            source_event_ref=source_event_ref,
            receipt_ref=receipt_ref,
        )
        _insert_source_event_locked(
            conn,
            contract_type=CONTEXT_RECEIPT_VERSION,
            principal=principal,
            payload=normalized,
            semantic_hash=semantic_hash,
            source_event_ref=source_event_ref,
            observation_ref=str(refs["observation_ref"]),
            current=current,
        )
        _insert_ingest_receipt_locked(
            conn,
            normalized=normalized,
            principal=principal,
            contract_type=CONTEXT_RECEIPT_VERSION,
            payload_hash=payload_hash,
            receipt_ref=receipt_ref,
            source_event_ref=source_event_ref,
            observation_ref=str(refs["observation_ref"]),
            result_status=str(refs["result_status"]),
            current=current,
        )
        conn.commit()
        return {"status": "accepted", **refs, "source_event_ref": source_event_ref, "raw_access": False}
    except ContractError as exc:
        if conn.in_transaction:
            conn.rollback()
        status = "conflict" if exc.reason_code in {
            "idempotency_conflict",
            "canonical_source_event_conflict",
            "external_receipt_binding_conflict",
        } else "rejected"
        return _result(status, exc.reason_code)
    except sqlite3.IntegrityError:
        if conn.in_transaction:
            conn.rollback()
        return _scoped_ingest_race_result(
            conn,
            contract_type=CONTEXT_RECEIPT_VERSION,
            principal_id=principal_id,
            source_ref=source_ref,
            idempotency_key=str(normalized.get("idempotency_key") or ""),
            payload_hash=payload_hash,
        )
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "database_write_failed")


def _ingest_controlled_evidence_locked(
    conn: sqlite3.Connection,
    raw_contract: bytes | str,
    *,
    verification_secret: bytes | str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not context_console_enabled(conn):
        return _result("disabled", "feature_flag_off")
    try:
        payload = _parse_closed_json(raw_contract, _CONTROLLED_EVIDENCE_FIELDS)
        normalized = _validate_controlled_evidence(conn, payload)
        preliminary_kind = {
            "user_confirmation": "user_confirmation",
            "file_saved": "file_adapter",
            "git_commit": "git_adapter",
            "external_receipt": "external_adapter",
        }[str(normalized["evidence_kind"])]
        preliminary_principal = _authenticate_principal(
            conn,
            principal_id=str(normalized["principal_id"]),
            key_id=str(normalized["key_id"]),
            verification_secret=verification_secret,
            expected_kinds={preliminary_kind},
            project_ref=str(normalized["project_ref"]),
        )
        _reject_document_mapping_principal_locked(
            conn,
            preliminary_principal.principal_id,
            preliminary_principal.key_id,
        )
        _verify_signature(normalized, verification_secret)
    except ContractError as exc:
        return _result("rejected", exc.reason_code)
    payload_hash = _payload_hash(normalized)
    current = _aware_utc(now)
    principal_id = str(normalized["principal_id"])
    source_ref = str(normalized["source_ref"])
    try:
        conn.execute("BEGIN IMMEDIATE")
        normalized = _validate_controlled_evidence(conn, payload)
        expected_kind = {
            "user_confirmation": "user_confirmation",
            "file_saved": "file_adapter",
            "git_commit": "git_adapter",
            "external_receipt": "external_adapter",
        }[str(normalized["evidence_kind"])]
        principal = _authenticate_principal(
            conn,
            principal_id=principal_id,
            key_id=str(normalized["key_id"]),
            verification_secret=verification_secret,
            expected_kinds={expected_kind},
            project_ref=str(normalized["project_ref"]),
        )
        _reject_document_mapping_principal_locked(
            conn,
            principal.principal_id,
            principal.key_id,
        )
        _verify_signature(normalized, verification_secret)

        source_event_ref = _canonical_source_event_ref(
            CONTROLLED_EVIDENCE_VERSION, principal.principal_id, normalized
        )
        semantic_hash = _source_event_semantic_hash(CONTROLLED_EVIDENCE_VERSION, normalized)
        source_replay = _source_event_guard_locked(
            conn, source_event_ref=source_event_ref, semantic_hash=semantic_hash
        )
        if str(normalized["evidence_kind"]) == "external_receipt":
            # Exact same-key replay is also an authorization boundary.  It
            # must validate (or safely repair) the immutable provider binding
            # before the idempotency path can return.
            _bind_external_receipt_locked(
                conn,
                principal_id=principal.principal_id,
                payload=normalized,
                current=current,
            )
        replay = _scoped_ingest_idempotency_locked(
            conn,
            contract_type=CONTROLLED_EVIDENCE_VERSION,
            principal_id=principal.principal_id,
            source_ref=str(normalized["source_ref"]),
            idempotency_key=str(normalized["idempotency_key"]),
            payload_hash=payload_hash,
        )
        if replay is not None:
            conn.commit()
            return replay
        if source_replay and source_replay.get("semantic_match") != "1":
            raise ContractError("canonical_source_event_conflict")

        # New-key canonical replays must pass the same freshness gate as a
        # first submission.  Exact same-key idempotent replay remains governed
        # by the scoped receipt lookup above.
        occurred = _parse_contract_time(str(normalized["occurred_at"]))
        _validate_freshness(occurred, current)
        receipt_ref = _derived_ref(
            "receipt",
            f"{CONTROLLED_EVIDENCE_VERSION}|{principal.principal_id}|{normalized['source_ref']}|"
            f"{normalized['idempotency_key']}|{payload_hash}",
        )
        if source_replay:
            conn.commit()
            return {
                "status": "replayed",
                "reason_code": "canonical_source_event_replay",
                "receipt_ref": source_replay["receipt_ref"],
                "observation_ref": source_replay["observation_ref"],
                "result_status": source_replay["result_status"],
                "source_event_ref": source_event_ref,
                "raw_access": False,
            }

        refs = _materialize_controlled_evidence(
            conn,
            normalized,
            payload_hash,
            current,
            principal,
            source_event_ref=source_event_ref,
            receipt_ref=receipt_ref,
        )
        _insert_source_event_locked(
            conn,
            contract_type=CONTROLLED_EVIDENCE_VERSION,
            principal=principal,
            payload=normalized,
            semantic_hash=semantic_hash,
            source_event_ref=source_event_ref,
            observation_ref=str(refs["observation_ref"]),
            current=current,
        )
        _insert_ingest_receipt_locked(
            conn,
            normalized=normalized,
            principal=principal,
            contract_type=CONTROLLED_EVIDENCE_VERSION,
            payload_hash=payload_hash,
            receipt_ref=receipt_ref,
            source_event_ref=source_event_ref,
            observation_ref=str(refs["observation_ref"]),
            result_status=str(refs["result_status"]),
            current=current,
        )
        conn.commit()
        return {"status": "accepted", **refs, "source_event_ref": source_event_ref, "raw_access": False}
    except ContractError as exc:
        if conn.in_transaction:
            conn.rollback()
        status = "conflict" if exc.reason_code in {
            "idempotency_conflict",
            "canonical_source_event_conflict",
            "external_receipt_binding_conflict",
        } else "rejected"
        return _result(status, exc.reason_code)
    except sqlite3.IntegrityError:
        if conn.in_transaction:
            conn.rollback()
        return _scoped_ingest_race_result(
            conn,
            contract_type=CONTROLLED_EVIDENCE_VERSION,
            principal_id=principal_id,
            source_ref=source_ref,
            idempotency_key=str(normalized.get("idempotency_key") or ""),
            payload_hash=payload_hash,
        )
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "database_write_failed")




def _document_mapping_principal_locked(
    conn: sqlite3.Connection,
    mapping: sqlite3.Row | Mapping[str, Any],
) -> Principal:
    principal_id = str(mapping["principal_id"] or "")
    key_id = str(mapping["key_id"] or "")
    project_ref = str(mapping["project_ref"] or "")
    row = conn.execute(
        "SELECT principal_kind,project_ref,key_fingerprint,status FROM context_principals "
        "WHERE principal_id=? AND key_id=?",
        (principal_id, key_id),
    ).fetchone()
    if not row or str(row["status"] or "") != "active":
        raise ContractError("document_mapping_principal_inactive")
    principal_kind = str(row["principal_kind"] or "")
    if principal_kind not in {"file_adapter", DOCUMENT_MAPPING_PRINCIPAL_KIND}:
        raise ContractError("document_mapping_principal_invalid")
    if str(row["project_ref"] or "") != project_ref:
        raise ContractError("document_mapping_principal_invalid")
    return Principal(
        principal_id=principal_id,
        key_id=key_id,
        principal_kind=principal_kind,
        project_ref=project_ref,
        target_ref="",
        target_scope="",
        key_fingerprint=str(row["key_fingerprint"] or ""),
    )


def _materialize_document_candidate_locked(
    conn: sqlite3.Connection,
    *,
    payload: Mapping[str, Any],
    source_event_ref: str,
    document_source_ref: str,
    current: datetime,
    principal: Principal,
) -> dict[str, Any]:
    observation_ref = _derived_ref("observation", f"document|{source_event_ref}")
    evidence_ref = _derived_ref("evidence", f"document|{source_event_ref}")
    candidate_ref = _derived_ref("candidate", f"document|{source_event_ref}")
    occurred = _parse_contract_time(str(payload["occurred_at"]))
    expiry = _iso(occurred + timedelta(days=UNVERIFIED_RETENTION_DAYS))
    receipt_ref = _derived_ref("receipt", f"document-owner-local|{source_event_ref}")
    conn.execute(
        "INSERT INTO context_observations(observation_ref,source_kind,source_ref,source_event_ref,principal_id,key_id,"
        "adapter_kind,receipt_ref,lifecycle_event,occurred_at,project_ref,workstream_ref,object_ref,version_ref,"
        "subject_ref,privacy_class,trust_partition,status,expires_at,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,'artifact_saved',?,?,?,?,?,?,?,'owner_local_unverified','active',?,?)",
        (
            observation_ref,
            "file_adapter",
            payload["source_ref"],
            source_event_ref,
            principal.principal_id,
            principal.key_id,
            principal.principal_kind,
            receipt_ref,
            payload["occurred_at"],
            payload["project_ref"],
            payload["workstream_ref"],
            payload["object_ref"],
            payload["version_ref"],
            payload["subject_ref"],
            payload["privacy_class"],
            expiry,
            _iso(current),
        ),
    )
    conn.execute(
        "INSERT INTO context_evidence(evidence_ref,observation_ref,evidence_kind,evidence_tier,state,state_cap,provenance_ref,"
        "source_ref,occurred_at,project_ref,workstream_ref,object_ref,version_ref,subject_ref,principal_id,key_id,adapter_kind,"
        "privacy_class,status,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?)",
        (
            evidence_ref,
            observation_ref,
            "file_saved",
            "P2",
            "artifact_saved",
            "artifact_saved",
            document_source_ref,
            payload["source_ref"],
            payload["occurred_at"],
            payload["project_ref"],
            payload["workstream_ref"],
            payload["object_ref"],
            payload["version_ref"],
            payload["subject_ref"],
            principal.principal_id,
            principal.key_id,
            principal.principal_kind,
            payload["privacy_class"],
            expiry,
            _iso(current),
        ),
    )
    conn.execute(
        "INSERT INTO context_observation_evidence_links(observation_ref,evidence_ref) VALUES(?,?)",
        (observation_ref, evidence_ref),
    )
    conn.execute(
        "INSERT INTO context_candidates(candidate_ref,observation_ref,desired_state,disposition,status,created_at,expires_at) "
        "VALUES(?,?,'artifact_saved','requires_confirmation','active',?,?)",
        (candidate_ref, observation_ref, _iso(current), expiry),
    )
    conn.execute(
        "INSERT INTO context_candidate_evidence(candidate_ref,evidence_ref,dependency_role) VALUES(?,?,'primary')",
        (candidate_ref, evidence_ref),
    )
    return {
        "receipt_ref": receipt_ref,
        "observation_ref": observation_ref,
        "evidence_ref": evidence_ref,
        "candidate_ref": candidate_ref,
        "verified_ref": "",
        "result_status": "candidate_only",
        "verification_state": "unverified",
        "memory_eligible": False,
        "projection_contract": DOCUMENT_OWNER_LOCAL_PROJECTION_VERSION,
    }


def _document_identity_ref(root_id: str, file_identity: str) -> str:
    return _derived_ref("document", f"{root_id}|{file_identity}")


def _document_helper_event_ref(root_id: str, helper_event_id: str) -> str:
    return _derived_ref("helperevent", f"{root_id}|{helper_event_id}")


def _document_content_version_ref(
    root_id: str,
    file_identity: str,
    content_version_identity: str,
) -> str:
    return _derived_ref("contentversion", f"{root_id}|{file_identity}|{content_version_identity}")


def _revoke_document_context_locked(
    conn: sqlite3.Connection,
    *,
    root_id: str,
    file_identity: str = "",
    reason_code: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _aware_utc(now)
    stamp = _iso(current)
    _validate_reason_code(reason_code)
    if not isinstance(root_id, str) or not re.fullmatch(r"root_[0-9a-f]{32}", root_id):
        raise ContractError("invalid_document_root")
    scope = "file" if file_identity else "root"
    document_identity_ref = _document_identity_ref(root_id, file_identity) if file_identity else ""
    tombstone_ref = _derived_ref(
        "documenttombstone",
        f"{scope}|{root_id}|{document_identity_ref}",
    )
    before = conn.total_changes
    conn.execute(
        "INSERT OR IGNORE INTO context_document_tombstones(tombstone_ref,scope,root_id,document_identity_ref,"
        "reason_code,status,created_at) VALUES(?,?,?,?,?,'active',?)",
        (tombstone_ref, scope, root_id, document_identity_ref, reason_code, stamp),
    )
    tombstone_created = conn.total_changes > before
    mapping_principal_rows = []
    if scope == "root":
        mapping_principal_rows = conn.execute(
            "SELECT DISTINCT principal_id,key_id FROM context_document_mappings "
            "WHERE root_id=? ORDER BY principal_id,key_id",
            (root_id,),
        ).fetchall()
        conn.execute(
            "UPDATE context_document_mappings SET status='revoked',revoked_at=?,revocation_reason_code=? "
            "WHERE root_id=? AND status='active'",
            (stamp, reason_code, root_id),
        )
        rows = conn.execute(
            "SELECT observation_ref FROM context_document_source_versions WHERE root_id=? AND status='active' "
            "ORDER BY observation_ref",
            (root_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT observation_ref FROM context_document_source_versions WHERE root_id=? "
            "AND document_identity_ref=? AND status='active' ORDER BY observation_ref",
            (root_id, document_identity_ref),
        ).fetchall()
    observation_refs = sorted({str(row["observation_ref"] or "") for row in rows if row["observation_ref"]})
    cascade = _zero_cascade()
    mapping_principals_revoked = 0
    for mapping_principal in mapping_principal_rows:
        principal_result = _revoke_context_principal_rows_locked(
            conn,
            str(mapping_principal["principal_id"] or ""),
            str(mapping_principal["key_id"] or ""),
            reason_code=reason_code,
            now=current,
        )
        mapping_principals_revoked += int(principal_result["principal_revoked"])
        _merge_cascade(cascade, principal_result["cascade"])
    for observation_ref in observation_refs:
        _merge_cascade(
            cascade,
            _cascade_observation_locked(
                conn,
                observation_ref,
                terminal_status="revoked",
                reason_code=reason_code,
                now=current,
            ),
        )
    if scope == "root":
        cursor = conn.execute(
            "UPDATE context_document_source_versions SET status='revoked',revoked_at=?,revocation_reason_code=? "
            "WHERE root_id=? AND status='active'",
            (stamp, reason_code, root_id),
        )
    else:
        cursor = conn.execute(
            "UPDATE context_document_source_versions SET status='revoked',revoked_at=?,revocation_reason_code=? "
            "WHERE root_id=? AND document_identity_ref=? AND status='active'",
            (stamp, reason_code, root_id, document_identity_ref),
        )
    return {
        "status": "completed" if observation_refs or tombstone_created else "replayed",
        "reason_code": reason_code,
        "scope": scope,
        "root_id": root_id,
        "tombstone_ref": tombstone_ref,
        "tombstone_created": tombstone_created,
        "source_versions_revoked": (
            int(cursor.rowcount or 0) + int(cascade.get("document_source_versions_revoked", 0))
        ),
        "document_mapping_principals_revoked": mapping_principals_revoked,
        "dependent_observation_count": len(observation_refs),
        "cascade": cascade,
        "raw_access": False,
    }


def revoke_document_context_root(
    conn: sqlite3.Connection,
    *,
    root_id: str,
    reason_code: str = "root_authorization_revoked",
    now: datetime | None = None,
) -> dict[str, Any]:
    with _context_authorization_lock(conn, exclusive=True):
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = _revoke_document_context_locked(
                conn,
                root_id=root_id,
                reason_code=reason_code,
                now=now,
            )
            conn.commit()
            return result
        except ContractError as exc:
            if conn.in_transaction:
                conn.rollback()
            return _result("rejected", exc.reason_code)
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
            return _result("failed", "document_revocation_failed")


def revoke_document_context_file(
    conn: sqlite3.Connection,
    *,
    root_id: str,
    file_identity: str,
    reason_code: str = "document_deleted",
    now: datetime | None = None,
) -> dict[str, Any]:
    with _context_authorization_lock(conn, exclusive=True):
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = _revoke_document_context_locked(
                conn,
                root_id=root_id,
                file_identity=file_identity,
                reason_code=reason_code,
                now=now,
            )
            conn.commit()
            return result
        except ContractError as exc:
            if conn.in_transaction:
                conn.rollback()
            return _result("rejected", exc.reason_code)
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
            return _result("failed", "document_revocation_failed")

def record_document_change_context_locked(
    conn: sqlite3.Connection,
    *,
    source_event_id: str,
    occurred_at: str,
    root_id: str,
    file_identity: str,
    helper_event_id: str,
    content_version_identity: str,
    version_count: int,
    change_kind: str,
    detail_level: str,
    project_label: str = "",
) -> dict[str, Any]:
    """Project a document event without owning or committing the transaction.

    Missing/inactive mappings fail closed while allowing the legacy document
    event to commit.  Any SQL or contract failure after an active mapping is
    selected is surfaced as ``failed``/``rejected`` so the caller can roll the
    source event and every derived row back together.
    """
    if not isinstance(source_event_id, str) or not source_event_id.startswith("fce_"):
        return _result("rejected", "invalid_document_source_event")
    if not isinstance(file_identity, str) or not file_identity:
        return _result("rejected", "invalid_document_identity")
    if not isinstance(helper_event_id, str) or not helper_event_id:
        return _result("rejected", "invalid_document_helper_event")
    if not isinstance(content_version_identity, str) or not content_version_identity:
        return _result("rejected", "invalid_document_content_version")
    try:
        occurred = _parse_contract_time(occurred_at)
        version_number = int(version_count)
        if version_number < 1:
            raise ContractError("invalid_document_version")
        if change_kind not in {"created", "modified", "renamed", "deleted"}:
            raise ContractError("invalid_document_change_kind")
        if detail_level not in {"detailed", "metadata_only", "protected"}:
            raise ContractError("invalid_document_detail_level")
        if not isinstance(root_id, str) or not re.fullmatch(r"root_[0-9a-f]{32}", root_id):
            raise ContractError("invalid_document_root")
    except (ContractError, TypeError, ValueError) as exc:
        reason = exc.reason_code if isinstance(exc, ContractError) else "invalid_document_version"
        return _result("rejected", reason)

    if change_kind == "deleted":
        try:
            return _revoke_document_context_locked(
                conn,
                root_id=root_id,
                file_identity=file_identity,
                reason_code="document_deleted",
                now=occurred,
            )
        except ContractError as exc:
            return _result("rejected", exc.reason_code)
        except sqlite3.Error:
            return _result("failed", "document_revocation_failed")

    if not context_console_enabled(conn):
        return _result("disabled", "feature_flag_off")
    try:
        mapping = _active_document_mapping_locked(conn, root_id=root_id, occurred_at=occurred)
        if not mapping:
            return _result("skipped", "explicit_document_mapping_required")
        refs = {
            "project_ref": str(mapping["project_ref"]),
            "workstream_ref": str(mapping["workstream_ref"]),
            "object_ref": str(mapping["object_ref"]),
            "version_ref": str(mapping["version_ref"]),
            "subject_ref": str(mapping["subject_ref"]),
        }
        try:
            _validate_registry_refs(conn, refs)
            principal = _document_mapping_principal_locked(conn, mapping)
        except ContractError:
            return _result("skipped", "document_mapping_inactive")
        document_identity_ref = _document_identity_ref(root_id, file_identity)
        helper_event_ref = _document_helper_event_ref(root_id, helper_event_id)
        content_version_ref = _document_content_version_ref(
            root_id,
            file_identity,
            content_version_identity,
        )
        mapping_ref = str(mapping["mapping_ref"])
        mapping_version = int(mapping["mapping_version"])
        # The helper event is the canonical observation identity.  Content
        # version, mapping version, principal/signing key, timestamps, legacy
        # source IDs, and derived change labels are payload metadata: changing
        # any of them for the same helper event must conflict rather than mint a
        # second source identity.  Distinct observed content versions still get
        # distinct identities because the helper emits a distinct event for each
        # observed version.
        document_source_ref = _derived_ref(
            "documentsource",
            f"{root_id}|{document_identity_ref}|{helper_event_ref}",
        )
        source_event_ref = _derived_ref(
            "sourceevent",
            f"{DOCUMENT_ADAPTER_CONTRACT_VERSION}|{document_source_ref}",
        )
        source_ref = _derived_ref("source", f"document-owner-local|{document_source_ref}")
        privacy_class = "restricted_local" if detail_level == "protected" else "private_local"
        payload: dict[str, Any] = {
            "source_ref": source_ref,
            "occurred_at": _iso(occurred),
            **refs,
            "privacy_class": privacy_class,
            "change_kind": change_kind,
            "mapping_ref": mapping_ref,
            "mapping_version": mapping_version,
            "document_source_ref": document_source_ref,
            "helper_event_ref": helper_event_ref,
            "content_version_ref": content_version_ref,
        }
        stable_payload_hash = _payload_hash(
            {
                "source_ref": source_ref,
                **refs,
                "privacy_class": privacy_class,
                "change_kind": change_kind,
                "mapping_ref": mapping_ref,
                "mapping_version": mapping_version,
                "document_source_ref": document_source_ref,
                "helper_event_ref": helper_event_ref,
                "content_version_ref": content_version_ref,
            }
        )
        replay = _source_event_guard_locked(
            conn,
            source_event_ref=source_event_ref,
            semantic_hash=stable_payload_hash,
        )
        if replay and replay.get("semantic_match") != "1":
            raise ContractError("canonical_source_event_conflict")
        if replay:
            source_version = conn.execute(
                "SELECT evidence_ref,candidate_ref FROM context_document_source_versions "
                "WHERE source_event_ref=?",
                (source_event_ref,),
            ).fetchone()
            return {
                "status": "replayed",
                "reason_code": "canonical_source_event_replay",
                "contract_version": DOCUMENT_ADAPTER_CONTRACT_VERSION,
                "source_event_ref": source_event_ref,
                "document_source_ref": document_source_ref,
                "observation_ref": replay["observation_ref"],
                "evidence_ref": str(source_version["evidence_ref"] or "") if source_version else "",
                "candidate_ref": str(source_version["candidate_ref"] or "") if source_version else "",
                "verified_ref": "",
                "result_status": "candidate_only",
                "verification_state": "unverified",
                "memory_eligible": False,
                "raw_access": False,
            }
        current = _aware_utc(None)
        materialized = _materialize_document_candidate_locked(
            conn,
            payload=payload,
            source_event_ref=source_event_ref,
            document_source_ref=document_source_ref,
            current=current,
            principal=principal,
        )
        _insert_source_event_locked(
            conn,
            contract_type=DOCUMENT_ADAPTER_CONTRACT_VERSION,
            principal=principal,
            payload=payload,
            semantic_hash=stable_payload_hash,
            source_event_ref=source_event_ref,
            observation_ref=materialized["observation_ref"],
            current=current,
        )
        conn.execute(
            "INSERT INTO context_document_source_versions(document_source_ref,source_event_ref,root_id,mapping_ref,"
            "mapping_version,document_identity_ref,helper_event_ref,content_version_ref,observation_ref,evidence_ref,"
            "candidate_ref,change_kind,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
            (
                document_source_ref,
                source_event_ref,
                root_id,
                mapping_ref,
                mapping_version,
                document_identity_ref,
                helper_event_ref,
                content_version_ref,
                materialized["observation_ref"],
                materialized["evidence_ref"],
                materialized["candidate_ref"],
                change_kind,
                _iso(current),
            ),
        )
        return {
            "status": "accepted",
            "contract_version": DOCUMENT_ADAPTER_CONTRACT_VERSION,
            "mapping_ref": mapping_ref,
            "mapping_version": mapping_version,
            "document_source_ref": document_source_ref,
            "source_event_ref": source_event_ref,
            **materialized,
            "raw_access": False,
        }
    except ContractError as exc:
        status = "conflict" if exc.reason_code == "canonical_source_event_conflict" else "rejected"
        return _result(status, exc.reason_code)
    except sqlite3.Error:
        return _result("failed", "document_context_write_failed")


def record_unverified_clue(
    conn: sqlite3.Connection,
    *,
    source_kind: str,
    source_ref: str,
    occurred_at: str,
    project_ref: str,
    workstream_ref: str,
    object_ref: str,
    version_ref: str,
    subject_ref: str,
    privacy_class: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    with _context_authorization_lock(conn, exclusive=False):
        return _record_unverified_clue_locked(
            conn,
            source_kind=source_kind,
            source_ref=source_ref,
            occurred_at=occurred_at,
            project_ref=project_ref,
            workstream_ref=workstream_ref,
            object_ref=object_ref,
            version_ref=version_ref,
            subject_ref=subject_ref,
            privacy_class=privacy_class,
            now=now,
        )


def _record_unverified_clue_locked(
    conn: sqlite3.Connection,
    *,
    source_kind: str,
    source_ref: str,
    occurred_at: str,
    project_ref: str,
    workstream_ref: str,
    object_ref: str,
    version_ref: str,
    subject_ref: str,
    privacy_class: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record content-free screen/OCR metadata as an expiring clue only."""
    if not context_console_enabled(conn):
        return _result("disabled", "feature_flag_off")
    try:
        if source_kind not in {"screen", "ocr"}:
            raise ContractError("invalid_clue_source")
        _validate_opaque_ref("source_ref", source_ref)
        occurred = _parse_contract_time(occurred_at)
        current = _aware_utc(now)
        if occurred > current + RECEIPT_FUTURE_SKEW:
            raise ContractError("receipt_from_future")
        if occurred < _today_and_yesterday_cutoff(conn, current):
            raise ContractError("unverified_clue_too_old")
        refs = {
            "project_ref": project_ref,
            "workstream_ref": workstream_ref,
            "object_ref": object_ref,
            "version_ref": version_ref,
            "subject_ref": subject_ref,
        }
        if privacy_class not in PRIVACY_CLASSES:
            raise ContractError("invalid_privacy_class")
        # Syntax only outside the transaction; registry state is re-read below.
        for kind, ref in (("project", project_ref), ("workstream", workstream_ref), ("object", object_ref),
                          ("version", version_ref), ("subject", subject_ref)):
            _validate_ref_for_kind(kind, ref)
    except ContractError as exc:
        return _result("rejected", exc.reason_code)

    normalized = {
        "source_kind": source_kind,
        "source_ref": source_ref,
        "occurred_at": _iso(occurred),
        **refs,
        "privacy_class": privacy_class,
    }
    payload_hash = _public_hash(normalized)
    source_event_ref = _derived_ref(
        "sourceevent",
        "UnverifiedClueV1|" + json.dumps(normalized, sort_keys=True, separators=(",", ":")),
    )
    observation_ref = _derived_ref("observation", source_event_ref)
    evidence_ref = _derived_ref("evidence", f"{source_event_ref}|{source_kind}")
    created_at = _iso(current)
    expires_at = _unverified_expiry(conn, occurred)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _validate_registry_refs(conn, refs)
        tombstone = conn.execute(
            "SELECT 1 FROM context_source_event_tombstones WHERE source_event_ref=?",
            (source_event_ref,),
        ).fetchone()
        if tombstone:
            raise ContractError("canonical_source_event_tombstoned")
        existing_event = conn.execute(
            "SELECT payload_hash,observation_ref,status FROM context_source_events WHERE source_event_ref=?",
            (source_event_ref,),
        ).fetchone()
        if existing_event:
            if str(existing_event["status"] or "") != "active":
                raise ContractError("canonical_source_event_tombstoned")
            if not hmac.compare_digest(str(existing_event["payload_hash"] or ""), payload_hash):
                raise ContractError("canonical_source_event_conflict")
            conn.commit()
            return {
                "status": "replayed",
                "reason_code": "canonical_source_event_replay",
                "source_event_ref": source_event_ref,
                "observation_ref": str(existing_event["observation_ref"]),
                "trust_partition": "unverified_clue",
                "verified_context_created": False,
                "raw_access": False,
            }
        conn.execute(
            "INSERT INTO context_observations(observation_ref,source_kind,source_ref,source_event_ref,principal_id,key_id,"
            "adapter_kind,receipt_ref,lifecycle_event,occurred_at,project_ref,workstream_ref,object_ref,version_ref,"
            "subject_ref,privacy_class,trust_partition,status,expires_at,created_at) "
            "VALUES(?,?,?,?,'principal_localcore','key_localcore',?,'','',?,?,?,?,?,?,?,'unverified_clue','active',?,?)",
            (
                observation_ref,
                source_kind,
                source_ref,
                source_event_ref,
                f"{source_kind}_clue",
                _iso(occurred),
                project_ref,
                workstream_ref,
                object_ref,
                version_ref,
                subject_ref,
                privacy_class,
                expires_at,
                created_at,
            ),
        )
        conn.execute(
            "INSERT INTO context_evidence(evidence_ref,observation_ref,evidence_kind,evidence_tier,state,state_cap,"
            "provenance_ref,source_ref,occurred_at,project_ref,workstream_ref,object_ref,version_ref,subject_ref,"
            "principal_id,key_id,adapter_kind,privacy_class,status,expires_at,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'principal_localcore','key_localcore',?,?,'active',?,?)",
            (
                evidence_ref,
                observation_ref,
                source_kind,
                "P5",
                "unverified_clue",
                "unverified_clue",
                source_ref,
                source_ref,
                _iso(occurred),
                project_ref,
                workstream_ref,
                object_ref,
                version_ref,
                subject_ref,
                f"{source_kind}_clue",
                privacy_class,
                expires_at,
                created_at,
            ),
        )
        conn.execute(
            "INSERT INTO context_observation_evidence_links(observation_ref,evidence_ref) VALUES(?,?)",
            (observation_ref, evidence_ref),
        )
        conn.execute(
            "INSERT INTO context_source_events(source_event_ref,contract_type,principal_id,key_id,source_ref,occurred_at,"
            "project_ref,workstream_ref,object_ref,version_ref,subject_ref,payload_hash,observation_ref,status,created_at) "
            "VALUES(?,'UnverifiedClueV1','principal_localcore','key_localcore',?,?,?,?,?,?,?,?,?,'active',?)",
            (
                source_event_ref,
                source_ref,
                _iso(occurred),
                project_ref,
                workstream_ref,
                object_ref,
                version_ref,
                subject_ref,
                payload_hash,
                observation_ref,
                created_at,
            ),
        )
        _index_unverified_clue(conn, observation_ref)
        conn.commit()
        return {
            "status": "accepted",
            "source_event_ref": source_event_ref,
            "observation_ref": observation_ref,
            "evidence_ref": evidence_ref,
            "trust_partition": "unverified_clue",
            "verification_state": "unverified",
            "verified_context_created": False,
            "expires_at": expires_at,
            "raw_access": False,
        }
    except ContractError as exc:
        if conn.in_transaction:
            conn.rollback()
        status = "conflict" if exc.reason_code == "canonical_source_event_conflict" else "rejected"
        return _result(status, exc.reason_code)
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "database_write_failed")


def _materialize_context_bundle_locked(
    conn: sqlite3.Connection,
    *,
    project_ref: str = "",
    start_at: str = "",
    end_at: str = "",
    query: str = "",
    include_clues: bool = True,
    document_dependency_only: bool = False,
    allow_unregistered_empty_project: bool = False,
    limit: int = 100,
    selected_context_refs: Sequence[str] | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    if not context_console_enabled(conn):
        return ({
            "contract_version": CONTEXT_BUNDLE_VERSION,
            "status": "disabled",
            "reason_code": "feature_flag_off",
            "raw_access": False,
            "verified_contexts": [],
            "unverified_clues": [],
        }, [])
    current = _aware_utc(now)
    try:
        if project_ref and not allow_unregistered_empty_project:
            _require_active_label(conn, project_ref, "project")
        start = _parse_optional_time(start_at)
        end = _parse_optional_time(end_at)
        if start and end and start >= end:
            raise ContractError("invalid_time_range")
        safe_query = _validate_search_query(query)
        limit = max(1, min(int(limit), 200))
    except (ContractError, TypeError, ValueError) as exc:
        reason = exc.reason_code if isinstance(exc, ContractError) else "invalid_limit"
        return ({
            "contract_version": CONTEXT_BUNDLE_VERSION,
            "status": "rejected",
            "reason_code": reason,
            "raw_access": False,
            "verified_contexts": [],
            "unverified_clues": [],
        }, [])
    selected_verified = _select_verified_contexts(
        conn,
        project_ref=project_ref,
        start_at=_iso(start) if start else "",
        end_at=_iso(end) if end else "",
        query=safe_query,
        document_dependency_only=document_dependency_only,
        limit=limit,
        now=current,
    )
    if selected_context_refs is not None:
        if (not isinstance(selected_context_refs, (list, tuple)) or not 1 <= len(selected_context_refs) <= 50
                or any(not isinstance(ref, str) or not re.fullmatch(r"context_[a-f0-9]{8,64}", ref) for ref in selected_context_refs)):
            return (_result("rejected", "invalid_context_selection"), [])
        wanted = set(selected_context_refs)
        selected_verified = [(private_ref, item) for private_ref, item in selected_verified if item["context_ref"] in wanted]
        if {item["context_ref"] for _, item in selected_verified} != wanted:
            return (_result("rejected", "selected_context_unavailable"), [])
    clues = (
        _select_unverified_clues(
            conn,
            project_ref=project_ref,
            start_at=_iso(start) if start else "",
            end_at=_iso(end) if end else "",
            query=safe_query,
            limit=limit,
            now=current,
        )
        if include_clues
        else []
    )
    # Database row order, insertion order, and wall-clock generation time do
    # not participate in bundle identity.
    selected_verified = sorted(
        selected_verified,
        key=lambda selected: (
            str(selected[1].get("occurred_at") or ""),
            str(selected[1].get("context_ref") or ""),
            str(selected[0]),
        ),
    )
    # Two private rows with the same public semantic projection must not create
    # duplicate public identities.  Keep one deterministic private membership
    # key while preserving the public projection as the only visible identity.
    unique_selected: list[tuple[str, dict[str, Any]]] = []
    seen_public_refs: set[str] = set()
    for internal_ref, item in selected_verified:
        public_ref = str(item.get("context_ref") or "")
        if public_ref in seen_public_refs:
            continue
        seen_public_refs.add(public_ref)
        unique_selected.append((internal_ref, item))
    selected_verified = unique_selected
    verified = [item for _, item in selected_verified]
    verified_source_refs = {str(item.get("source_ref") or "") for item in verified}
    clues = [item for item in clues if str(item.get("source_ref") or "") not in verified_source_refs]
    clues = sorted(
        clues,
        key=lambda item: (
            str(item.get("occurred_at") or ""),
            str(item.get("source_ref") or ""),
        ),
    )
    filters = {
        "project_ref": project_ref,
        "start_at": _iso(start) if start else "",
        "end_at": _iso(end) if end else "",
        "query_applied": bool(safe_query),
        "query_fingerprint": _hash_text(safe_query) if safe_query else "",
        "include_clues": bool(include_clues),
    }
    if document_dependency_only:
        filters["document_dependency_only"] = True
    stable_projection = {
        "contract_version": CONTEXT_BUNDLE_VERSION,
        "raw_access": False,
        "filters": filters,
        "verified_contexts": verified,
        "unverified_clues": clues,
    }
    bundle_hash = _public_hash(stable_projection)
    result = {
        "contract_version": CONTEXT_BUNDLE_VERSION,
        "status": "available",
        "raw_access": False,
        "generated_at": _iso(current),
        "filters": filters,
        "verified_contexts": verified,
        "unverified_clues": clues,
        "bundle_hash": bundle_hash,
        # This exact object is the consumable, hash-covered payload.  It has no
        # generated_at field and is safe to persist/fetch verbatim.
        "frozen_payload": stable_projection,
    }
    _validate_public_bundle_projection(result["frozen_payload"])
    return result, selected_verified


def _build_context_bundle_locked(
    conn: sqlite3.Connection,
    **kwargs: Any,
) -> dict[str, Any]:
    bundle, _ = _materialize_context_bundle_locked(conn, **kwargs)
    return bundle

def _target_policy_projection(
    *,
    project_ref: str,
    target_ref: str,
    target_scope: str,
    export_policy: str,
    allowed_privacy_classes: Sequence[str],
) -> dict[str, Any]:
    return {
        "policy_version": "TargetPrivacyPolicyV1",
        "project_ref": project_ref,
        "target_ref": target_ref,
        "target_scope": target_scope,
        "export_policy": export_policy,
        "allowed_privacy_classes": sorted(set(allowed_privacy_classes)),
    }


def _load_target_policy(
    conn: sqlite3.Connection,
    target_ref: str,
    *,
    project_ref: str = "",
) -> dict[str, Any]:
    target = _require_active_label(conn, target_ref, "target")
    target_project = str(target["project_ref"] or "")
    if not target_project:
        raise ContractError("target_project_mismatch")
    if project_ref and target_project != project_ref:
        raise ContractError("target_project_mismatch")
    row = conn.execute(
        "SELECT target_scope,allowed_privacy_classes,export_policy,policy_declared "
        "FROM context_target_policies WHERE target_ref=?",
        (target_ref,),
    ).fetchone()
    if not row:
        raise ContractError("target_policy_missing")
    if int(row["policy_declared"] or 0) != 1:
        raise ContractError("target_policy_undeclared")
    target_scope = str(row["target_scope"] or "")
    export_policy = str(row["export_policy"] or "")
    try:
        decoded = json.loads(str(row["allowed_privacy_classes"] or "[]"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContractError("target_policy_invalid") from exc
    if not isinstance(decoded, list) or not decoded or any(not isinstance(value, str) for value in decoded):
        raise ContractError("target_policy_invalid")
    allowed = sorted(set(decoded))
    if len(allowed) != len(decoded) or any(value not in PRIVACY_CLASSES for value in allowed):
        raise ContractError("target_policy_invalid")
    if target_scope not in TARGET_SCOPES or export_policy not in TARGET_EXPORT_POLICIES:
        raise ContractError("target_policy_invalid")
    if target_scope == "local" and export_policy != "local_only":
        raise ContractError("local_target_requires_local_only")
    if target_scope == "external":
        if export_policy != "explicit_export":
            raise ContractError("external_target_requires_explicit_export")
        if "restricted_local" in allowed:
            raise ContractError("restricted_local_export_forbidden")
    projection = _target_policy_projection(
        project_ref=target_project,
        target_ref=target_ref,
        target_scope=target_scope,
        export_policy=export_policy,
        allowed_privacy_classes=allowed,
    )
    return {
        **projection,
        "allowed_privacy_classes_json": json.dumps(allowed, separators=(",", ":")),
        "privacy_policy_hash": _public_hash(projection),
    }


def _validate_memory_source_eligible(
    conn: sqlite3.Connection,
    *,
    verified_ref: str,
    subject_ref: str,
    now: datetime,
) -> sqlite3.Row:
    subject = _require_active_label(conn, subject_ref, "subject")
    if str(subject["subject_scope"] or "") != "owner":
        raise ContractError("third_party_subject_forbidden")
    row = _load_verified_graph(conn, verified_ref)
    if not row:
        raise ContractError("verified_context_required")
    if str(row["subject_ref"] or "") != subject_ref:
        raise ContractError("subject_mismatch")
    if str(subject["project_ref"] or "") != str(row["project_ref"] or ""):
        raise ContractError("subject_project_mismatch")
    _validate_verified_graph(conn, row, now)
    if str(row["privacy_class"] or "") == "restricted_local":
        raise ContractError("restricted_local_memory_forbidden")
    if str(row["verification_scope"] or "") != "memory" or int(row["memory_eligible"] or 0) != 1:
        raise ContractError("memory_scope_not_eligible")
    if str(row["source_kind"] or "") != "user_confirmation":
        raise ContractError("operational_evidence_memory_forbidden")
    evidence_rows = conn.execute(
        "SELECT e.evidence_tier,e.evidence_kind,e.adapter_kind,e.principal_id,e.key_id,e.provenance_ref,"
        "e.status,e.expires_at,e.subject_ref,e.project_ref,e.privacy_class FROM context_candidate_evidence ce "
        "JOIN context_evidence e ON e.evidence_ref=ce.evidence_ref WHERE ce.candidate_ref=? ORDER BY e.evidence_ref",
        (str(row["candidate_ref"]),),
    ).fetchall()
    if not evidence_rows:
        raise ContractError("provenance_missing")
    for evidence in evidence_rows:
        if str(evidence["status"] or "") != "active":
            raise ContractError("provenance_revoked")
        if _parse_contract_time(str(evidence["expires_at"])) <= now:
            raise ContractError("provenance_expired")
        if str(evidence["evidence_tier"] or "") not in {"P0", "P1"}:
            raise ContractError("memory_evidence_tier_insufficient")
        if str(evidence["evidence_kind"] or "") != "user_confirmation":
            raise ContractError("operational_evidence_memory_forbidden")
        if str(evidence["adapter_kind"] or "") != "user_confirmation":
            raise ContractError("operational_evidence_memory_forbidden")
        if str(evidence["subject_ref"] or "") != subject_ref:
            raise ContractError("subject_mismatch")
        if str(evidence["project_ref"] or "") != str(row["project_ref"] or ""):
            raise ContractError("provenance_project_mismatch")
        if str(evidence["privacy_class"] or "") == "restricted_local":
            raise ContractError("restricted_local_memory_forbidden")
        if not str(evidence["provenance_ref"] or ""):
            raise ContractError("provenance_missing")
        principal = conn.execute(
            "SELECT status,principal_kind,project_ref,target_ref,target_scope FROM context_principals "
            "WHERE principal_id=? AND key_id=?",
            (str(evidence["principal_id"] or ""), str(evidence["key_id"] or "")),
        ).fetchone()
        if not principal or str(principal["status"] or "") != "active":
            raise ContractError("provenance_principal_revoked")
        if str(principal["principal_kind"] or "") != "user_confirmation":
            raise ContractError("operational_evidence_memory_forbidden")
        if str(principal["project_ref"] or "") != str(row["project_ref"] or ""):
            raise ContractError("provenance_principal_project_mismatch")
        if str(principal["target_ref"] or "") or str(principal["target_scope"] or ""):
            raise ContractError("provenance_principal_mismatch")
    return row


def _validate_memory_candidate_eligible(
    conn: sqlite3.Connection,
    candidate: sqlite3.Row | Mapping[str, Any],
    now: datetime,
) -> sqlite3.Row:
    if str(candidate["status"] or "") != "candidate_only" or str(candidate["revoked_at"] or ""):
        raise ContractError("memory_candidate_revoked")
    if _parse_contract_time(str(candidate["expires_at"])) <= now:
        raise ContractError("memory_candidate_expired")
    verified_ref = str(candidate["verified_ref"] or "")
    subject_ref = str(candidate["subject_ref"] or "")
    row = _validate_memory_source_eligible(
        conn,
        verified_ref=verified_ref,
        subject_ref=subject_ref,
        now=now,
    )
    if str(candidate["project_ref"] or "") != str(row["project_ref"] or ""):
        raise ContractError("memory_candidate_project_mismatch")
    if str(candidate["subject_ref"] or "") != str(row["subject_ref"] or ""):
        raise ContractError("subject_mismatch")
    return row


def _memory_candidate_result(
    conn: sqlite3.Connection,
    *,
    candidate_ref: str,
    verified_row: sqlite3.Row | Mapping[str, Any],
    subject_ref: str,
    expires_at: str,
    status: str,
) -> dict[str, Any]:
    public_context = _public_verified_context(conn, verified_row)
    return {
        "contract_version": MEMORY_CANDIDATE_VERSION,
        "status": status,
        **({"reason_code": "idempotent_replay"} if status == "replayed" else {}),
        "candidate_status": "candidate_only",
        "memory_candidate_ref": candidate_ref,
        "context_ref": public_context["context_ref"],
        "subject_ref": subject_ref,
        "expires_at": expires_at,
        "raw_access": False,
        "memory_write_actions": False,
        "writer_enabled": False,
        "direct_write": False,
    }


def _create_memory_candidate_locked(
    conn: sqlite3.Connection,
    *,
    verified_ref: str,
    subject_ref: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not context_console_enabled(conn):
        return _result("disabled", "feature_flag_off")
    current = _aware_utc(now)
    try:
        _validate_internal_ref("verified", verified_ref)
        _validate_opaque_ref("subject_ref", subject_ref)
    except ContractError as exc:
        return _result("rejected", exc.reason_code)
    candidate_ref = _derived_ref("memory", f"{verified_ref}|{subject_ref}")
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM context_memory_candidates WHERE memory_candidate_ref=?",
            (candidate_ref,),
        ).fetchone()
        try:
            row = _validate_memory_source_eligible(
                conn,
                verified_ref=verified_ref,
                subject_ref=subject_ref,
                now=current,
            )
        except ContractError as exc:
            # A replay after scope/provenance withdrawal is terminalized instead
            # of silently reactivating an old candidate-only row.
            if existing and str(existing["status"] or "") == "candidate_only":
                conn.execute(
                    "UPDATE context_memory_candidates SET status='revoked',revoked_at=?,revocation_reason_code=? "
                    "WHERE memory_candidate_ref=? AND status='candidate_only'",
                    (_iso(current), exc.reason_code, candidate_ref),
                )
                conn.commit()
                return _result("rejected", exc.reason_code)
            raise
        expires_at = str(row["expires_at"])
        if existing:
            exact = (
                str(existing["verified_ref"] or "") == verified_ref
                and str(existing["subject_ref"] or "") == subject_ref
                and str(existing["project_ref"] or "") == str(row["project_ref"] or "")
            )
            if not exact:
                conn.rollback()
                return _result("conflict", "memory_candidate_conflict")
            _validate_memory_candidate_eligible(conn, existing, current)
            conn.commit()
            return _memory_candidate_result(
                conn,
                candidate_ref=candidate_ref,
                verified_row=row,
                subject_ref=subject_ref,
                expires_at=str(existing["expires_at"]),
                status="replayed",
            )
        conn.execute(
            "INSERT INTO context_memory_candidates(memory_candidate_ref,verified_ref,subject_ref,project_ref,status,"
            "created_at,expires_at,revoked_at,revocation_reason_code) "
            "VALUES(?,?,?,?,'candidate_only',?,?,NULL,'')",
            (candidate_ref, verified_ref, subject_ref, str(row["project_ref"]), _iso(current), expires_at),
        )
        conn.commit()
        return _memory_candidate_result(
            conn,
            candidate_ref=candidate_ref,
            verified_row=row,
            subject_ref=subject_ref,
            expires_at=expires_at,
            status="candidate_only",
        )
    except ContractError as exc:
        if conn.in_transaction:
            conn.rollback()
        return _result("rejected", exc.reason_code)
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "database_write_failed")


def _prepare_delivery_locked(
    conn: sqlite3.Connection,
    *,
    target_ref: str,
    project_ref: str = "",
    start_at: str = "",
    end_at: str = "",
    query: str = "",
    now: datetime | None = None,
    selected_context_refs: Sequence[str] | None = None,
) -> dict[str, Any]:
    if not context_console_enabled(conn):
        return _result("disabled", "feature_flag_off")
    current = _aware_utc(now)
    try:
        target = _require_active_label(conn, target_ref, "target")
        target_project = str(target["project_ref"] or "")
        effective_project = project_ref or target_project
        if not target_project or effective_project != target_project:
            raise ContractError("target_project_mismatch")
        _load_target_policy(conn, target_ref, project_ref=effective_project)
    except ContractError as exc:
        return _result("rejected", exc.reason_code)

    bundle, selected_members = _materialize_context_bundle_locked(
        conn,
        project_ref=effective_project,
        start_at=start_at,
        end_at=end_at,
        query=query,
        include_clues=False,
        selected_context_refs=selected_context_refs,
        limit=200,
        now=current,
    )
    if bundle.get("status") != "available":
        return bundle
    bundle_hash = str(bundle["bundle_hash"])
    bundle_ref = _derived_ref("bundle", bundle_hash)
    created_at = _iso(current)
    frozen_payload = bundle["frozen_payload"]
    _validate_public_bundle_projection(frozen_payload)
    if frozen_payload.get("unverified_clues") != []:
        return _result("rejected", "trusted_delivery_clues_forbidden")
    frozen_json = json.dumps(frozen_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    filters = bundle.get("filters") or {}
    manifest_shape = {
        "bundle_hash": bundle_hash,
        "project_ref": str(filters.get("project_ref") or ""),
        "start_at": str(filters.get("start_at") or ""),
        "end_at": str(filters.get("end_at") or ""),
        "query_fingerprint": str(filters.get("query_fingerprint") or ""),
        "item_count": len(bundle["verified_contexts"]),
        "stable_projection_json": frozen_json,
        "frozen_payload_json": frozen_json,
    }
    expected_members = {verified_ref: _public_hash(item) for verified_ref, item in selected_members}
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_active_label(conn, effective_project, "project")
        policy = _load_target_policy(conn, target_ref, project_ref=effective_project)
        allowed_privacy = set(policy["allowed_privacy_classes"])
        for internal_ref, item in selected_members:
            privacy = str(item.get("privacy_class") or "")
            if privacy not in allowed_privacy:
                raise ContractError("target_privacy_policy_forbidden")
            if str(policy["target_scope"]) == "external" and privacy == "restricted_local":
                raise ContractError("restricted_local_export_forbidden")
            graph = _load_verified_graph(conn, internal_ref)
            if not graph:
                raise ContractError("bundle_source_invalid")
            try:
                _validate_verified_graph(conn, graph, current)
            except ContractError as exc:
                raise ContractError("bundle_source_invalid") from exc
            if _public_hash(_public_verified_context(conn, graph)) != _public_hash(item):
                raise ContractError("bundle_source_changed")

        manifest = conn.execute(
            "SELECT * FROM context_bundle_manifests WHERE bundle_ref=?", (bundle_ref,)
        ).fetchone()
        if manifest:
            if str(manifest["status"] or "") != "active":
                raise ContractError("bundle_restricted")
            for key, expected in manifest_shape.items():
                actual = int(manifest[key]) if key == "item_count" else str(manifest[key] or "")
                if actual != expected:
                    raise ContractError("bundle_manifest_conflict")
        else:
            conn.execute(
                "INSERT INTO context_bundle_manifests(bundle_ref,bundle_hash,project_ref,start_at,end_at,query_fingerprint,"
                "target_ref,item_count,stable_projection_json,frozen_payload_json,status,created_at) "
                "VALUES(?,?,?,?,?,?, '',?,?,?,'active',?)",
                (
                    bundle_ref,
                    bundle_hash,
                    manifest_shape["project_ref"],
                    manifest_shape["start_at"],
                    manifest_shape["end_at"],
                    manifest_shape["query_fingerprint"],
                    manifest_shape["item_count"],
                    frozen_json,
                    frozen_json,
                    created_at,
                ),
            )
            manifest = conn.execute(
                "SELECT * FROM context_bundle_manifests WHERE bundle_ref=?", (bundle_ref,)
            ).fetchone()
        for member_ref, member_hash in expected_members.items():
            conn.execute(
                "INSERT INTO context_bundle_members(bundle_ref,member_kind,member_ref,member_hash,status) "
                "VALUES(?,?,?,?,'active') ON CONFLICT(bundle_ref,member_kind,member_ref) DO NOTHING",
                (bundle_ref, "verified_context", member_ref, member_hash),
            )
        actual_rows = conn.execute(
            "SELECT member_kind,member_ref,member_hash,status FROM context_bundle_members WHERE bundle_ref=?",
            (bundle_ref,),
        ).fetchall()
        actual_members = {
            str(row["member_ref"]): str(row["member_hash"])
            for row in actual_rows
            if str(row["member_kind"]) == "verified_context" and str(row["status"]) == "active"
        }
        if len(actual_members) != len(expected_members) or actual_members != expected_members:
            raise ContractError("bundle_member_conflict")

        latest = conn.execute(
            "SELECT * FROM context_deliveries WHERE project_ref=? AND target_ref=? "
            "ORDER BY attempt_no DESC,created_at DESC LIMIT 1",
            (effective_project, target_ref),
        ).fetchone()
        if latest and str(latest["bundle_ref"] or "") == bundle_ref and str(latest["state"] or "") != "failed":
            _validate_delivery_bundle_live(conn, latest, current)
            conn.commit()
            return {
                "status": "replayed",
                "reason_code": "active_attempt_replay",
                "delivery_ref": str(latest["delivery_ref"]),
                "attempt_no": int(latest["attempt_no"]),
                "state": str(latest["state"]),
                "bundle_ref": bundle_ref,
                "bundle_hash": bundle_hash,
                "project_ref": effective_project,
                "target_ref": target_ref,
                "target_scope": str(latest["target_scope"]),
                "export_policy": str(latest["export_policy"]),
                "privacy_policy_hash": str(latest["privacy_policy_hash"]),
                "frozen_payload": frozen_payload,
                "raw_access": False,
            }
        attempt_no = int(latest["attempt_no"] or 0) + 1 if latest else 1
        delivery_ref = _derived_ref(
            "delivery", f"{effective_project}|{target_ref}|attempt:{attempt_no}|{bundle_hash}"
        )
        conn.execute(
            "INSERT INTO context_deliveries(delivery_ref,bundle_ref,bundle_hash,project_ref,target_ref,attempt_no,"
            "target_scope,export_policy,allowed_privacy_classes_json,privacy_policy_hash,state,prepared_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'prepared',?,?,?)",
            (
                delivery_ref,
                bundle_ref,
                bundle_hash,
                effective_project,
                target_ref,
                attempt_no,
                policy["target_scope"],
                policy["export_policy"],
                policy["allowed_privacy_classes_json"],
                policy["privacy_policy_hash"],
                created_at,
                created_at,
                created_at,
            ),
        )
        prepared_idempotency = _derived_ref("idem", f"prepared|{delivery_ref}")
        prepared_receipt = _derived_ref("receipt", f"prepared|{delivery_ref}")
        conn.execute(
            "INSERT INTO context_delivery_receipts(receipt_ref,delivery_ref,principal_id,key_id,idempotency_key,payload_hash,"
            "bundle_ref,attempt_no,project_ref,target_ref,target_scope,export_policy,privacy_policy_hash,principal_role,"
            "transition,occurred_at,bundle_hash,provider_receipt_ref,failure_reason_code,result_state,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?, '', '', 'prepared',?)",
            (
                prepared_receipt,
                delivery_ref,
                "principal_localcore",
                "key_localcore",
                prepared_idempotency,
                _hash_text(f"prepared|{delivery_ref}|{bundle_hash}|{policy['privacy_policy_hash']}"),
                bundle_ref,
                attempt_no,
                effective_project,
                target_ref,
                policy["target_scope"],
                policy["export_policy"],
                policy["privacy_policy_hash"],
                "system",
                created_at,
                bundle_hash,
                created_at,
            ),
        )
        conn.commit()
        return {
            "status": "prepared",
            "delivery_ref": delivery_ref,
            "attempt_no": attempt_no,
            "bundle_ref": bundle_ref,
            "bundle_hash": bundle_hash,
            "project_ref": effective_project,
            "target_ref": target_ref,
            "verified_context_count": len(bundle["verified_contexts"]),
            "unverified_clue_count": 0,
            "target_scope": policy["target_scope"],
            "export_policy": policy["export_policy"],
            "privacy_policy_hash": policy["privacy_policy_hash"],
            "frozen_payload": frozen_payload,
            "raw_access": False,
        }
    except ContractError as exc:
        if conn.in_transaction:
            conn.rollback()
        return _result("rejected", exc.reason_code)
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "database_write_failed")


def fetch_prepared_delivery(
    conn: sqlite3.Connection,
    *,
    delivery_ref: str = "",
    bundle_ref: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch a hash-covered public payload by immutable attempt or bundle identity."""
    current = _aware_utc(now)
    try:
        if not delivery_ref and not bundle_ref:
            raise ContractError("delivery_or_bundle_ref_required")
        if delivery_ref:
            _validate_opaque_ref("delivery_ref", delivery_ref)
        if bundle_ref:
            _validate_opaque_ref("bundle_ref", bundle_ref)
        with _context_authorization_lock(conn, exclusive=False):
            delivery = None
            if delivery_ref:
                delivery = conn.execute(
                    "SELECT * FROM context_deliveries WHERE delivery_ref=?", (delivery_ref,)
                ).fetchone()
                if not delivery:
                    raise ContractError("delivery_not_found")
                actual_bundle_ref = str(delivery["bundle_ref"] or "")
                if bundle_ref and bundle_ref != actual_bundle_ref:
                    raise ContractError("delivery_bundle_mismatch")
                bundle_ref = actual_bundle_ref
            manifest = conn.execute(
                "SELECT * FROM context_bundle_manifests WHERE bundle_ref=?", (bundle_ref,)
            ).fetchone()
            if not manifest:
                raise ContractError("bundle_manifest_missing")
            if delivery is not None:
                _validate_delivery_bundle_live(conn, delivery, current)
            else:
                _validate_bundle_manifest_live(conn, manifest, current)
            payload = json.loads(str(manifest["frozen_payload_json"] or "{}"))
            _validate_public_bundle_projection(payload)
            if _public_hash(payload) != str(manifest["bundle_hash"] or ""):
                raise ContractError("frozen_payload_hash_mismatch")
            result: dict[str, Any] = {
                "status": "available",
                "bundle_ref": bundle_ref,
                "bundle_hash": str(manifest["bundle_hash"]),
                "frozen_payload": payload,
                "observed_at": _iso(current),
                "raw_access": False,
            }
            if delivery is not None:
                result.update(
                    {
                        "delivery_ref": delivery_ref,
                        "attempt_no": int(delivery["attempt_no"]),
                        "state": str(delivery["state"]),
                        "project_ref": str(delivery["project_ref"]),
                        "target_ref": str(delivery["target_ref"]),
                        "target_scope": str(delivery["target_scope"]),
                        "export_policy": str(delivery["export_policy"]),
                        "privacy_policy_hash": str(delivery["privacy_policy_hash"]),
                    }
                )
            return result
    except (ContractError, json.JSONDecodeError) as exc:
        reason = exc.reason_code if isinstance(exc, ContractError) else "frozen_payload_invalid"
        return _result("rejected", reason)


def _principal_delivery_role(principal_kind: str) -> str:
    if principal_kind == "bundle_consumer":
        return "consumer"
    if principal_kind == "delivery_provider":
        return "provider"
    raise ContractError("principal_kind_not_authorized")


def _authorize_delivery_contract(
    principal: Principal,
    normalized: Mapping[str, Any],
    delivery: sqlite3.Row | Mapping[str, Any],
    manifest: sqlite3.Row | Mapping[str, Any],
) -> None:
    expected = {
        "bundle_ref": str(delivery["bundle_ref"] or ""),
        "attempt_no": int(delivery["attempt_no"] or 0),
        "project_ref": str(delivery["project_ref"] or ""),
        "target_ref": str(delivery["target_ref"] or ""),
        "target_scope": str(delivery["target_scope"] or ""),
        "export_policy": str(delivery["export_policy"] or ""),
        "privacy_policy_hash": str(delivery["privacy_policy_hash"] or ""),
        "bundle_hash": str(delivery["bundle_hash"] or ""),
    }
    if expected["project_ref"] != str(manifest["project_ref"] or ""):
        raise ContractError("delivery_project_mismatch")
    for field, value in expected.items():
        if normalized.get(field) != value:
            if field == "bundle_hash":
                raise ContractError("bundle_hash_mismatch")
            raise ContractError(f"delivery_{field.removesuffix('_ref')}_mismatch")
    role = _principal_delivery_role(principal.principal_kind)
    if normalized.get("principal_role") != role:
        raise ContractError("principal_role_mismatch")
    if not principal.project_ref:
        raise ContractError("delivery_principal_project_required")
    if principal.project_ref != expected["project_ref"]:
        raise ContractError("principal_project_mismatch")
    if not principal.target_ref:
        raise ContractError("delivery_principal_target_required")
    if principal.target_ref != expected["target_ref"]:
        raise ContractError("principal_target_mismatch")
    if principal.target_scope not in TARGET_SCOPES:
        raise ContractError("delivery_principal_scope_required")
    if principal.target_scope != expected["target_scope"]:
        raise ContractError("principal_target_scope_mismatch")


def _validate_delivery_replay_locked(
    conn: sqlite3.Connection,
    *,
    existing: sqlite3.Row | Mapping[str, Any],
    normalized: Mapping[str, Any],
    payload_hash: str,
    delivery: sqlite3.Row | Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    if not hmac.compare_digest(str(existing["payload_hash"] or ""), payload_hash):
        raise ContractError("idempotency_conflict")
    for field in (
        "bundle_ref",
        "attempt_no",
        "project_ref",
        "target_ref",
        "target_scope",
        "export_policy",
        "privacy_policy_hash",
        "principal_role",
        "transition",
        "bundle_hash",
    ):
        stored = int(existing[field] or 0) if field == "attempt_no" else str(existing[field] or "")
        if stored != normalized.get(field):
            raise ContractError("delivery_replay_binding_mismatch")
    if str(delivery["state"] or "") != str(existing["result_state"] or ""):
        raise ContractError("delivery_replay_stale")
    _validate_delivery_bundle_live(conn, delivery, now)
    return {
        "status": "replayed",
        "reason_code": "idempotent_replay",
        "receipt_ref": str(existing["receipt_ref"]),
        "delivery_ref": str(normalized["delivery_ref"]),
        "attempt_no": int(delivery["attempt_no"]),
        "bundle_ref": str(delivery["bundle_ref"]),
        "state": str(existing["result_state"]),
        "raw_access": False,
    }


def _ingest_delivery_receipt_locked(
    conn: sqlite3.Connection,
    raw_contract: bytes | str,
    *,
    verification_secret: bytes | str,
    now: datetime | None = None,
    _retry_on_integrity: bool = True,
) -> dict[str, Any]:
    """Apply a fully bound acknowledgement to exactly one delivery attempt."""
    if not context_console_enabled(conn):
        return _result("disabled", "feature_flag_off")
    try:
        payload = _parse_closed_json(raw_contract, _DELIVERY_RECEIPT_FIELDS)
        normalized = _validate_delivery_receipt(payload)
        preliminary_transition = str(normalized["transition"])
        preliminary_kinds = (
            {"bundle_consumer"}
            if preliminary_transition == "consumed"
            else {"delivery_provider"}
            if preliminary_transition == "delivered"
            else {"bundle_consumer", "delivery_provider"}
        )
        _authenticate_principal(
            conn,
            principal_id=str(normalized["principal_id"]),
            key_id=str(normalized["key_id"]),
            verification_secret=verification_secret,
            expected_kinds=preliminary_kinds,
            project_ref="",
        )
        _verify_signature(normalized, verification_secret)
    except ContractError as exc:
        return _result("rejected", exc.reason_code)
    payload_hash = _payload_hash(normalized)
    current = _aware_utc(now)
    principal_id = str(normalized["principal_id"])
    try:
        conn.execute("BEGIN IMMEDIATE")
        normalized = _validate_delivery_receipt(payload)
        transition = str(normalized["transition"])
        expected_kinds = (
            {"bundle_consumer"}
            if transition == "consumed"
            else {"delivery_provider"}
            if transition == "delivered"
            else {"bundle_consumer", "delivery_provider"}
        )
        delivery = conn.execute(
            "SELECT * FROM context_deliveries WHERE delivery_ref=?",
            (normalized["delivery_ref"],),
        ).fetchone()
        if not delivery:
            raise ContractError("delivery_not_found")
        manifest = conn.execute(
            "SELECT * FROM context_bundle_manifests WHERE bundle_ref=?",
            (str(delivery["bundle_ref"]),),
        ).fetchone()
        if not manifest:
            raise ContractError("bundle_manifest_missing")
        principal = _authenticate_principal(
            conn,
            principal_id=principal_id,
            key_id=str(normalized["key_id"]),
            verification_secret=verification_secret,
            expected_kinds=expected_kinds,
            project_ref=str(normalized["project_ref"]),
        )
        _verify_signature(normalized, verification_secret)
        _authorize_delivery_contract(principal, normalized, delivery, manifest)
        occurred = _parse_contract_time(normalized["occurred_at"])

        existing_receipt = conn.execute(
            "SELECT * FROM context_delivery_receipts WHERE delivery_ref=? AND principal_id=? AND idempotency_key=?",
            (normalized["delivery_ref"], principal.principal_id, normalized["idempotency_key"]),
        ).fetchone()
        if existing_receipt:
            replay = _validate_delivery_replay_locked(
                conn,
                existing=existing_receipt,
                normalized=normalized,
                payload_hash=payload_hash,
                delivery=delivery,
                now=current,
            )
            conn.commit()
            return replay

        # Freshness is a gate for a new receipt. An exact replay is not a new
        # receipt, but is still re-authenticated and revalidated above.
        _validate_freshness(occurred, current)
        current_state = str(delivery["state"] or "")
        _validate_delivery_transition(current_state, transition)
        _validate_delivery_bundle_live(conn, delivery, current)
        lower_bound = _parse_contract_time(str(delivery["prepared_at"]))
        if current_state == "consumed" and delivery["consumed_at"]:
            lower_bound = _parse_contract_time(str(delivery["consumed_at"]))
        if occurred < lower_bound:
            raise ContractError("delivery_timestamp_regression")

        if transition == "consumed":
            cursor = conn.execute(
                "UPDATE context_deliveries SET state='consumed',consumed_at=?,consumer_receipt_ref=?,updated_at=? "
                "WHERE delivery_ref=? AND state='prepared'",
                (
                    normalized["occurred_at"],
                    normalized["provider_receipt_ref"],
                    _iso(current),
                    normalized["delivery_ref"],
                ),
            )
            result_state = "consumed"
        elif transition == "delivered":
            if normalized["provider_receipt_ref"] == str(delivery["consumer_receipt_ref"] or ""):
                raise ContractError("independent_provider_receipt_required")
            consumed_receipt = conn.execute(
                "SELECT principal_id,key_id,principal_role,bundle_ref,attempt_no,project_ref,target_ref,target_scope,"
                "export_policy,privacy_policy_hash,bundle_hash FROM context_delivery_receipts "
                "WHERE delivery_ref=? AND transition='consumed' ORDER BY created_at DESC LIMIT 1",
                (normalized["delivery_ref"],),
            ).fetchone()
            if not consumed_receipt or str(consumed_receipt["principal_role"] or "") != "consumer":
                raise ContractError("consumer_receipt_missing")
            for field in (
                "bundle_ref",
                "attempt_no",
                "project_ref",
                "target_ref",
                "target_scope",
                "export_policy",
                "privacy_policy_hash",
                "bundle_hash",
            ):
                prior = int(consumed_receipt[field] or 0) if field == "attempt_no" else str(consumed_receipt[field] or "")
                if prior != normalized[field]:
                    raise ContractError("consumer_receipt_binding_mismatch")
            consumer_principal_id = str(consumed_receipt["principal_id"] or "")
            consumer_key_id = str(consumed_receipt["key_id"] or "")
            if consumer_principal_id == principal.principal_id:
                raise ContractError("independent_delivery_provider_required")
            consumer_key = conn.execute(
                "SELECT key_fingerprint FROM context_principals WHERE principal_id=? AND key_id=?",
                (consumer_principal_id, consumer_key_id),
            ).fetchone()
            if consumer_key and hmac.compare_digest(
                str(consumer_key["key_fingerprint"] or ""), principal.key_fingerprint
            ):
                raise ContractError("independent_delivery_provider_required")
            cursor = conn.execute(
                "UPDATE context_deliveries SET state='delivered',delivered_at=?,provider_receipt_ref=?,updated_at=? "
                "WHERE delivery_ref=? AND state='consumed'",
                (
                    normalized["occurred_at"],
                    normalized["provider_receipt_ref"],
                    _iso(current),
                    normalized["delivery_ref"],
                ),
            )
            result_state = "delivered"
        else:
            cursor = conn.execute(
                "UPDATE context_deliveries SET state='failed',failed_at=?,failure_reason_code=?,updated_at=? "
                "WHERE delivery_ref=? AND state IN ('prepared','consumed')",
                (
                    normalized["occurred_at"],
                    normalized["failure_reason_code"],
                    _iso(current),
                    normalized["delivery_ref"],
                ),
            )
            result_state = "failed"
        if int(cursor.rowcount or 0) != 1:
            raise ContractError("delivery_state_changed")

        receipt_ref = _derived_ref(
            "receipt",
            f"delivery|{normalized['delivery_ref']}|{principal.principal_id}|{normalized['idempotency_key']}|{payload_hash}",
        )
        conn.execute(
            "INSERT INTO context_delivery_receipts(receipt_ref,delivery_ref,principal_id,key_id,idempotency_key,payload_hash,"
            "bundle_ref,attempt_no,project_ref,target_ref,target_scope,export_policy,privacy_policy_hash,principal_role,"
            "transition,occurred_at,bundle_hash,provider_receipt_ref,failure_reason_code,result_state,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_ref,
                normalized["delivery_ref"],
                principal.principal_id,
                principal.key_id,
                normalized["idempotency_key"],
                payload_hash,
                normalized["bundle_ref"],
                normalized["attempt_no"],
                normalized["project_ref"],
                normalized["target_ref"],
                normalized["target_scope"],
                normalized["export_policy"],
                normalized["privacy_policy_hash"],
                normalized["principal_role"],
                transition,
                normalized["occurred_at"],
                normalized["bundle_hash"],
                normalized["provider_receipt_ref"],
                normalized["failure_reason_code"],
                result_state,
                _iso(current),
            ),
        )
        conn.commit()
        return {
            "status": "accepted",
            "receipt_ref": receipt_ref,
            "delivery_ref": normalized["delivery_ref"],
            "attempt_no": int(delivery["attempt_no"]),
            "bundle_ref": str(delivery["bundle_ref"]),
            "state": result_state,
            "raw_access": False,
        }
    except ContractError as exc:
        if conn.in_transaction:
            conn.rollback()
        status = "conflict" if exc.reason_code == "idempotency_conflict" else "rejected"
        return _result(status, exc.reason_code)
    except sqlite3.IntegrityError:
        if conn.in_transaction:
            conn.rollback()
        if _retry_on_integrity:
            return _ingest_delivery_receipt_locked(
                conn,
                raw_contract,
                verification_secret=verification_secret,
                now=now,
                _retry_on_integrity=False,
            )
        return _result("failed", "concurrent_write_conflict")
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "database_write_failed")


def _delete_context_observation_locked(
    conn: sqlite3.Connection,
    *,
    observation_ref: str,
    reason_code: str = "user_deleted",
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        _validate_internal_ref("observation", observation_ref)
        _validate_reason_code(reason_code)
    except ContractError as exc:
        return _result("rejected", exc.reason_code)
    current = _aware_utc(now)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM context_observations WHERE observation_ref=?", (observation_ref,)
        ).fetchone()
        if not row:
            conn.rollback()
            return _result("not_found", "observation_not_found")
        if str(row["status"]) in {"deleted", "expired", "revoked"}:
            conn.commit()
            return {
                "status": "already_revoked",
                "observation_ref": observation_ref,
                "raw_access": False,
                "cascade": _zero_cascade(),
            }
        cascade = _cascade_observation_locked(
            conn,
            observation_ref,
            terminal_status="deleted",
            reason_code=reason_code,
            now=current,
        )
        conn.commit()
        return {
            "status": "completed",
            "observation_ref": observation_ref,
            "raw_access": False,
            "cascade": cascade,
        }
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "deletion_cascade_failed")


def _revoke_context_observations_for_range_locked(
    conn: sqlite3.Connection,
    start_at: str,
    end_at: str,
    *,
    reason_code: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Join an already-owned transaction; used only for explicit manual deletion."""
    current = _aware_utc(now)
    rows = conn.execute(
        "SELECT observation_ref FROM context_observations WHERE status='active' "
        "AND datetime(occurred_at)>=datetime(?) AND datetime(occurred_at)<datetime(?)",
        (start_at, end_at),
    ).fetchall()
    total = _zero_cascade()
    for row in rows:
        cascade = _cascade_observation_locked(
            conn,
            str(row["observation_ref"]),
            terminal_status="deleted",
            reason_code=reason_code,
            now=current,
        )
        for key, value in cascade.items():
            total[key] = int(total.get(key, 0)) + int(value or 0)
    return {"observations": len(rows), **total}


def preview_context_retention(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    current = _aware_utc(now)
    clue_cutoff = _today_and_yesterday_cutoff(conn, current)
    timezone_name = _context_timezone_name(conn)
    stamp = _iso(current)
    counts = {
        "unverified_observations": _count_query(
            conn,
            "SELECT COUNT(*) FROM context_observations WHERE status='active' AND trust_partition='unverified_clue' "
            "AND (datetime(occurred_at)<datetime(?) OR datetime(expires_at)<=datetime(?))",
            (_iso(clue_cutoff), stamp),
        ),
        "verified_observations": _count_query(
            conn,
            "SELECT COUNT(*) FROM context_observations WHERE status='active' AND trust_partition='verified_eligible' "
            "AND datetime(expires_at)<=datetime(?)",
            (stamp,),
        ),
        "evidence": _count_query(
            conn,
            "SELECT COUNT(*) FROM context_evidence WHERE status='active' AND datetime(expires_at)<=datetime(?)",
            (stamp,),
        ),
        "verified_contexts": _count_query(
            conn,
            "SELECT COUNT(*) FROM context_verified WHERE status='active' AND datetime(expires_at)<=datetime(?)",
            (stamp,),
        ),
        "product_wins": _count_query(
            conn,
            "SELECT COUNT(*) FROM context_product_wins WHERE datetime(expires_at)<=datetime(?)",
            (stamp,),
        ),
    }
    return {
        "status": "dry_run",
        "raw_access": False,
        "cutoffs": {
            "unverified_observations": _iso(clue_cutoff),
            "verified_dependencies": stamp,
            "product_wins": stamp,
        },
        "timezone_name": timezone_name,
        "local_day_semantics": "iana_calendar_day",
        "would_expire_or_delete": counts,
    }


def _cleanup_context_retention_locked(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    current = _aware_utc(now)
    clue_cutoff = _today_and_yesterday_cutoff(conn, current)
    stamp = _iso(current)
    result = {
        "unverified_observations_expired": 0,
        "verified_observations_expired": 0,
        "evidence_expired": 0,
        "verified_contexts_expired": 0,
        "product_wins_deleted": 0,
        "memory_candidates_revoked": 0,
        "bundle_members_revoked": 0,
        "withdrawal_receipts_created": 0,
        "delivery_failures_recorded": 0,
        "source_event_tombstones_created": 0,
    }
    try:
        conn.execute("BEGIN IMMEDIATE")
        observation_rows = conn.execute(
            "SELECT observation_ref,trust_partition FROM context_observations WHERE status='active' AND ("
            "(trust_partition='unverified_clue' AND datetime(occurred_at)<datetime(?)) "
            "OR datetime(expires_at)<=datetime(?)) ORDER BY observation_ref",
            (_iso(clue_cutoff), stamp),
        ).fetchall()
        for row in observation_rows:
            trust_partition = str(row["trust_partition"] or "")
            reason = (
                "unverified_retention_expired"
                if trust_partition == "unverified_clue"
                else "observation_retention_expired"
            )
            cascade = _cascade_observation_locked(
                conn,
                str(row["observation_ref"]),
                terminal_status="expired",
                reason_code=reason,
                now=current,
            )
            if trust_partition == "unverified_clue":
                result["unverified_observations_expired"] += 1
            else:
                result["verified_observations_expired"] += 1
            _merge_cascade(result, cascade)

        # Defensive closure for legacy/corrupt rows where Evidence expires
        # before its owning Observation.  Expiring the source observation is
        # the smallest safe closure and also creates the source-event tombstone.
        evidence_rows = conn.execute(
            "SELECT DISTINCT observation_ref FROM context_evidence WHERE status='active' "
            "AND datetime(expires_at)<=datetime(?) ORDER BY observation_ref",
            (stamp,),
        ).fetchall()
        for row in evidence_rows:
            observation_ref = str(row["observation_ref"])
            active = conn.execute(
                "SELECT 1 FROM context_observations WHERE observation_ref=? AND status='active'",
                (observation_ref,),
            ).fetchone()
            if active:
                count = _count_query(
                    conn,
                    "SELECT COUNT(*) FROM context_evidence WHERE observation_ref=? AND status='active' "
                    "AND datetime(expires_at)<=datetime(?)",
                    (observation_ref, stamp),
                )
                cascade = _cascade_observation_locked(
                    conn,
                    observation_ref,
                    terminal_status="expired",
                    reason_code="evidence_retention_expired",
                    now=current,
                )
                result["evidence_expired"] += count
                _merge_cascade(result, cascade)

        expired_verified = conn.execute(
            "SELECT verified_ref FROM context_verified WHERE status='active' "
            "AND datetime(expires_at)<=datetime(?) ORDER BY verified_ref",
            (stamp,),
        ).fetchall()
        for row in expired_verified:
            cascade = _expire_verified_locked(conn, str(row["verified_ref"]), current)
            result["verified_contexts_expired"] += 1
            _merge_cascade(result, cascade)
        cursor = conn.execute(
            "DELETE FROM context_product_wins WHERE datetime(expires_at)<=datetime(?)",
            (stamp,),
        )
        result["product_wins_deleted"] = int(cursor.rowcount or 0)
        conn.commit()
        return {"status": "completed", "raw_access": False, **result}
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return {"status": "failed", "reason_code": "context_retention_failed", "raw_access": False, **result}


def record_product_win(
    conn: sqlite3.Connection,
    *,
    win_ref: str,
    occurred_at: str,
    project_ref: str,
    outcome_code: str,
    source_ref: str,
    minutes_saved: int = 0,
) -> dict[str, Any]:
    try:
        _validate_opaque_ref("win_ref", win_ref)
        _validate_opaque_ref("source_ref", source_ref)
        _require_active_label(conn, project_ref, "project")
        occurred = _parse_contract_time(occurred_at)
        if outcome_code not in PRODUCT_WIN_OUTCOMES:
            raise ContractError("invalid_product_win_outcome")
        minutes = int(minutes_saved)
        if minutes < 0 or minutes > 480:
            raise ContractError("invalid_minutes_saved")
    except (ContractError, TypeError, ValueError) as exc:
        reason = exc.reason_code if isinstance(exc, ContractError) else "invalid_minutes_saved"
        return _result("rejected", reason)
    try:
        local_date, expires_local_date, expires_at, timezone_name = _local_retention_window(
            conn,
            occurred,
            PRODUCT_WIN_RETENTION_DAYS,
        )
    except ContractError as exc:
        return _result("rejected", exc.reason_code)
    created_at = _utc_now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT occurred_at,local_date,project_ref,outcome_code,minutes_saved,source_ref,"
            "expires_local_date,timezone_name,expires_at "
            "FROM context_product_wins WHERE win_ref=?",
            (win_ref,),
        ).fetchone()
        if existing:
            exact = (
                str(existing["occurred_at"]) == _iso(occurred)
                and str(existing["project_ref"]) == project_ref
                and str(existing["outcome_code"]) == outcome_code
                and int(existing["minutes_saved"] or 0) == minutes
                and str(existing["source_ref"]) == source_ref
            )
            if not exact:
                conn.rollback()
                return _result("conflict", "product_win_conflict")
            conn.commit()
            return {
                "status": "replayed",
                "reason_code": "idempotent_replay",
                "win_ref": win_ref,
                "retention_days": PRODUCT_WIN_RETENTION_DAYS,
                "local_date": str(existing["local_date"] or local_date),
                "expires_local_date": str(existing["expires_local_date"] or expires_local_date),
                "timezone_name": str(existing["timezone_name"] or timezone_name),
                "expires_at": str(existing["expires_at"]),
                "raw_access": False,
            }
        conn.execute(
            "INSERT INTO context_product_wins(win_ref,occurred_at,local_date,project_ref,outcome_code,minutes_saved,"
            "source_ref,created_at,expires_local_date,timezone_name,expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                win_ref,
                _iso(occurred),
                local_date,
                project_ref,
                outcome_code,
                minutes,
                source_ref,
                created_at,
                expires_local_date,
                timezone_name,
                expires_at,
            ),
        )
        conn.commit()
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "database_write_failed")
    return {
        "status": "recorded",
        "win_ref": win_ref,
        "retention_days": PRODUCT_WIN_RETENTION_DAYS,
        "local_date": local_date,
        "expires_local_date": expires_local_date,
        "timezone_name": timezone_name,
        "expires_at": expires_at,
        "raw_access": False,
    }


def product_win_status(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    current = _aware_utc(now)
    row = conn.execute(
        "SELECT COUNT(*) AS count,COALESCE(SUM(minutes_saved),0) AS minutes FROM context_product_wins "
        "WHERE datetime(expires_at)>datetime(?)",
        (_iso(current),),
    ).fetchone()
    return {
        "status": "available",
        "active_win_count": int(row["count"] or 0),
        "minutes_saved": int(row["minutes"] or 0),
        "retention_days": PRODUCT_WIN_RETENTION_DAYS,
        "configured_timezone_name": _context_timezone_name(conn),
        "local_day_semantics": "iana_calendar_day",
        "raw_access": False,
    }


def start_product_experiment(
    conn: sqlite3.Connection,
    *,
    experiment_ref: str,
    confirmation: str,
    now: datetime | None = None,
    minimum_days: int = PRODUCT_EXPERIMENT_MIN_DAYS,
    maximum_days: int = PRODUCT_EXPERIMENT_MAX_DAYS,
) -> dict[str, Any]:
    try:
        _validate_opaque_ref("experiment_ref", experiment_ref)
        if confirmation != "START_LOCAL_PRODUCT_EXPERIMENT":
            raise ContractError("experiment_start_confirmation_required")
        minimum = int(minimum_days)
        maximum = int(maximum_days)
        if (
            minimum != PRODUCT_EXPERIMENT_MIN_DAYS
            or maximum != PRODUCT_EXPERIMENT_MAX_DAYS
        ):
            raise ContractError("invalid_experiment_window")
    except (ContractError, TypeError, ValueError) as exc:
        reason = exc.reason_code if isinstance(exc, ContractError) else "invalid_experiment_window"
        return _result("rejected", reason)
    # Injected time is deterministic test/replay input. Production time is a
    # trust input and must not be captured before BEGIN IMMEDIATE: the write
    # lock can wait across a local-midnight boundary.
    injected_current = _aware_utc(now) if now is not None else None
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not context_console_enabled(conn):
            raise ContractError("feature_flag_off")
        timezone_name = _context_timezone_name(conn)
        current = injected_current if injected_current is not None else _aware_utc(None)
        started = _local_today(conn, now=current, timezone_name=timezone_name)
        active = conn.execute(
            "SELECT experiment_ref FROM context_product_experiments "
            "WHERE status='running' LIMIT 1"
        ).fetchone()
        if active:
            reason = (
                "experiment_already_exists"
                if hmac.compare_digest(str(active["experiment_ref"] or ""), experiment_ref)
                else "experiment_already_running"
            )
            raise ContractError(reason)
        conn.execute(
            "INSERT INTO context_product_experiments(experiment_ref,status,started_local_date,ended_local_date,"
            "minimum_days,maximum_days,timezone_name,created_at,updated_at) "
            "VALUES(?,'running',?,NULL,?,?,?,?,?)",
            (
                experiment_ref,
                started.isoformat(),
                minimum,
                maximum,
                timezone_name,
                _iso(current),
                _iso(current),
            ),
        )
        conn.commit()
    except ContractError as exc:
        if conn.in_transaction:
            conn.rollback()
        if exc.reason_code == "feature_flag_off":
            status = "disabled"
        elif exc.reason_code in {"experiment_already_exists", "experiment_already_running"}:
            status = "conflict"
        else:
            status = "rejected"
        return _result(status, exc.reason_code)
    except sqlite3.IntegrityError:
        if conn.in_transaction:
            conn.rollback()
        active = conn.execute(
            "SELECT experiment_ref FROM context_product_experiments "
            "WHERE status='running' LIMIT 1"
        ).fetchone()
        reason = (
            "experiment_already_running"
            if active
            and not hmac.compare_digest(str(active["experiment_ref"] or ""), experiment_ref)
            else "experiment_already_exists"
        )
        return _result("conflict", reason)
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "database_write_failed")
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return product_experiment_status(conn, experiment_ref=experiment_ref, now=current)


def _product_experiment_thresholds() -> dict[str, Any]:
    return {
        "minimum_complete_attempts": PRODUCT_EXPERIMENT_MIN_ATTEMPTS,
        "useful_rate_minimum": PRODUCT_EXPERIMENT_USEFUL_RATE_THRESHOLD,
        "median_resume_seconds_strictly_less_than": PRODUCT_EXPERIMENT_MEDIAN_RESUME_THRESHOLD_SECONDS,
    }


def _unknown_product_experiment_status(
    reason_code: str,
    *,
    experiment_ref: str = "",
) -> dict[str, Any]:
    return {
        "status": "unknown",
        "reason_code": reason_code,
        "experiment_started": None,
        "experiment_ref": experiment_ref or None,
        "started_local_date": None,
        "evaluated_local_date": None,
        "evidence_cutoff_local_date": None,
        "timezone_name": None,
        "local_day_semantics": "iana_calendar_day",
        "elapsed_natural_days": None,
        "minimum_days": PRODUCT_EXPERIMENT_MIN_DAYS,
        "maximum_days": PRODUCT_EXPERIMENT_MAX_DAYS,
        "attempt_count": None,
        "rated_attempt_count": None,
        "timed_attempt_count": None,
        "complete_attempt_count": None,
        "missing_useful_count": None,
        "missing_resume_count": None,
        "useful_count": None,
        "not_useful_count": None,
        "useful_rate": None,
        "median_resume_seconds": None,
        "evidence_status": "unknown",
        "review_readiness": "pending",
        "completion_readiness": "pending",
        "decision_status": "pending",
        "value_gate_status": "pending",
        "value_gate_passed": None,
        "thresholds": _product_experiment_thresholds(),
        "raw_access": False,
    }


def _public_product_experiment_unknown(
    read_state: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if read_state not in {"busy", "unknown", "unavailable", "disabled"}:
        read_state = "unknown"
    return {
        "contract_version": PRODUCT_EXPERIMENT_STATUS_VERSION,
        "read_state": read_state,
        "observed_at": _iso(_aware_utc(now)),
        "status": None,
        "experiment_started": None,
        "experiment_ref": None,
        "attempt_count": None,
        "complete_attempt_count": None,
        "useful_rate": None,
        "median_resume_seconds": None,
        "raw_access": False,
    }


def _public_product_experiment_available_unknown(
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    payload = _public_product_experiment_unknown("unknown", now=now)
    payload["read_state"] = "available"
    payload["status"] = "unknown"
    return payload


def public_product_experiment_status(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the strict public scorecard without internal governance fields."""

    injected_observed = _aware_utc(now) if now is not None else None
    observed = injected_observed if injected_observed is not None else _aware_utc(None)
    try:
        # The whole public projection shares the same ordering boundary as
        # feature disable and destructive revocation.  Production time is a
        # trust input, so refresh it only after the shared lease is granted.
        with _context_authorization_lock(conn, exclusive=False):
            observed = (
                injected_observed
                if injected_observed is not None
                else _aware_utc(None)
            )
            if not context_console_enabled(conn):
                return _public_product_experiment_unknown("disabled", now=observed)
            internal = product_experiment_status(conn, now=observed)
            # Defense in depth for a legacy/direct settings writer that does
            # not participate in the authorization lease.  Never publish an
            # available scorecard after observing the gate close.
            if not context_console_enabled(conn):
                return _public_product_experiment_unknown("disabled", now=observed)

            status = str(internal.get("status") or "")
            allowed_statuses = {
                "not_started",
                "collecting",
                "incomplete",
                "extended",
                "passed",
                "not_passed",
                "insufficient",
            }
            if internal.get("raw_access") is not False:
                return _public_product_experiment_available_unknown(now=observed)
            if status == "unknown":
                return _public_product_experiment_available_unknown(now=observed)
            if status not in allowed_statuses:
                return _public_product_experiment_available_unknown(now=observed)

            experiment_started = internal.get("experiment_started")
            attempt_count = internal.get("attempt_count")
            complete_attempt_count = internal.get("complete_attempt_count")
            useful_rate = internal.get("useful_rate")
            median_resume_seconds = internal.get("median_resume_seconds")
            if not isinstance(experiment_started, bool):
                return _public_product_experiment_available_unknown(now=observed)
            if (
                isinstance(attempt_count, bool)
                or not isinstance(attempt_count, int)
                or attempt_count < 0
            ):
                return _public_product_experiment_available_unknown(now=observed)
            if (
                isinstance(complete_attempt_count, bool)
                or not isinstance(complete_attempt_count, int)
                or complete_attempt_count < 0
                or complete_attempt_count > attempt_count
            ):
                return _public_product_experiment_available_unknown(now=observed)
            for metric in (useful_rate, median_resume_seconds):
                if metric is not None and (
                    isinstance(metric, bool)
                    or not isinstance(metric, (int, float))
                    or not math.isfinite(float(metric))
                ):
                    return _public_product_experiment_available_unknown(now=observed)
            if useful_rate is not None and not 0.0 <= float(useful_rate) <= 1.0:
                return _public_product_experiment_available_unknown(now=observed)
            if median_resume_seconds is not None and float(median_resume_seconds) < 0.0:
                return _public_product_experiment_available_unknown(now=observed)

            running = status in {"collecting", "incomplete", "extended"}
            internal_ref = internal.get("experiment_ref")
            if experiment_started:
                if not isinstance(internal_ref, str) or not internal_ref:
                    return _public_product_experiment_available_unknown(now=observed)
                try:
                    _validate_opaque_ref("experiment_ref", internal_ref)
                except ContractError:
                    return _public_product_experiment_available_unknown(now=observed)
            if status == "not_started" and experiment_started:
                return _public_product_experiment_available_unknown(now=observed)
            if status != "not_started" and not experiment_started:
                return _public_product_experiment_available_unknown(now=observed)

            return {
                "contract_version": PRODUCT_EXPERIMENT_STATUS_VERSION,
                "read_state": "available",
                "observed_at": _iso(observed),
                "status": status,
                "experiment_started": experiment_started,
                "experiment_ref": internal_ref if running else None,
                "attempt_count": attempt_count,
                "complete_attempt_count": complete_attempt_count,
                "useful_rate": float(useful_rate) if useful_rate is not None else None,
                "median_resume_seconds": (
                    float(median_resume_seconds)
                    if median_resume_seconds is not None
                    else None
                ),
                "raw_access": False,
            }
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        read_state = "busy" if "locked" in message or "busy" in message else "unavailable"
        return _public_product_experiment_unknown(read_state, now=observed)
    except (sqlite3.Error, ContractError, RuntimeError, TypeError, ValueError):
        return _public_product_experiment_unknown("unavailable", now=observed)


def _product_attempt_useful_value(attempt: Mapping[str, Any]) -> bool | None:
    useful = attempt["useful"]
    outcome = attempt["outcome"]
    if outcome not in {None, "useful", "not_useful"}:
        raise ContractError("invalid_experiment_attempt_evidence")
    if useful is not None:
        if isinstance(useful, bool):
            normalized = int(useful)
        elif isinstance(useful, int) and useful in {0, 1}:
            normalized = useful
        else:
            raise ContractError("invalid_experiment_attempt_evidence")
        expected_outcome = "useful" if normalized else "not_useful"
        if outcome is not None and outcome != expected_outcome:
            raise ContractError("invalid_experiment_attempt_evidence")
        return bool(normalized)
    if outcome == "useful":
        return True
    if outcome == "not_useful":
        return False
    return None


def _product_attempt_resume_value(attempt: Mapping[str, Any]) -> float | None:
    value = attempt["resume_seconds"]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("invalid_experiment_attempt_evidence")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 86_400.0:
        raise ContractError("invalid_experiment_attempt_evidence")
    return normalized


def _canonical_product_experiment_date(value: Any, reason_code: str) -> date:
    if not isinstance(value, str):
        raise ContractError(reason_code)
    try:
        normalized = date.fromisoformat(value)
    except ValueError as exc:
        raise ContractError(reason_code) from exc
    if normalized.isoformat() != value:
        raise ContractError(reason_code)
    return normalized


def _product_attempt_local_date(attempt: Mapping[str, Any]) -> date:
    return _canonical_product_experiment_date(
        attempt["local_date"],
        "invalid_experiment_attempt_evidence",
    )


def _product_experiment_metrics(attempts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    normalized_attempts = [
        (
            _product_attempt_useful_value(attempt),
            _product_attempt_resume_value(attempt),
        )
        for attempt in attempts
    ]
    attempt_count = len(normalized_attempts)
    rated_count = sum(1 for useful_value, _ in normalized_attempts if useful_value is not None)
    timed_count = sum(1 for _, seconds_value in normalized_attempts if seconds_value is not None)
    complete_count = sum(
        1
        for useful_value, seconds_value in normalized_attempts
        if useful_value is not None and seconds_value is not None
    )
    missing_useful_count = attempt_count - rated_count
    missing_resume_count = attempt_count - timed_count
    useful_count = sum(1 for useful_value, _ in normalized_attempts if useful_value is True)
    not_useful_count = sum(1 for useful_value, _ in normalized_attempts if useful_value is False)
    useful_rate = (
        useful_count / attempt_count
        if attempt_count > 0 and missing_useful_count == 0
        else None
    )
    resume_values = [
        seconds_value
        for _, seconds_value in normalized_attempts
        if seconds_value is not None
    ]
    median_resume = (
        statistics.median(resume_values)
        if attempt_count > 0 and missing_resume_count == 0
        else None
    )
    has_missing_evidence = missing_useful_count > 0 or missing_resume_count > 0
    return {
        "attempt_count": attempt_count,
        "rated_attempt_count": rated_count,
        "timed_attempt_count": timed_count,
        "complete_attempt_count": complete_count,
        "missing_useful_count": missing_useful_count,
        "missing_resume_count": missing_resume_count,
        "useful_count": useful_count if rated_count else None,
        "not_useful_count": not_useful_count if rated_count else None,
        "useful_rate": useful_rate,
        "median_resume_seconds": median_resume,
        "has_missing_evidence": has_missing_evidence,
        "evidence_status": (
            "empty"
            if attempt_count == 0
            else ("incomplete" if has_missing_evidence else "complete")
        ),
    }


def _product_experiment_attempt_rows(
    conn: sqlite3.Connection,
    experiment_ref: str,
    cutoff: date,
    *,
    started: date | None = None,
    maximum_date: date | None = None,
) -> list[sqlite3.Row]:
    rows = list(
        conn.execute(
            "SELECT attempt_ref,event_id,local_date,useful,outcome,resume_seconds "
            "FROM context_product_experiment_attempts WHERE experiment_ref=? "
            "ORDER BY local_date,attempt_ref",
            (experiment_ref,),
        ).fetchall()
    )
    selected: list[sqlite3.Row] = []
    for row in rows:
        attempt_date = _product_attempt_local_date(row)
        _product_attempt_useful_value(row)
        _product_attempt_resume_value(row)
        if started is not None and attempt_date < started:
            raise ContractError("invalid_experiment_attempt_evidence")
        if maximum_date is not None and attempt_date > maximum_date:
            raise ContractError("invalid_experiment_attempt_evidence")
        if attempt_date <= cutoff:
            selected.append(row)
    return selected


def _product_value_gate_passed(metrics: Mapping[str, Any]) -> bool:
    return bool(
        float(metrics["useful_rate"]) >= PRODUCT_EXPERIMENT_USEFUL_RATE_THRESHOLD
        and float(metrics["median_resume_seconds"])
        < PRODUCT_EXPERIMENT_MEDIAN_RESUME_THRESHOLD_SECONDS
    )


def _find_product_experiment_terminal(
    attempts: Sequence[Mapping[str, Any]],
    *,
    started: date,
    minimum_days: int,
    maximum_days: int,
    evaluated_local_date: date,
) -> tuple[date, str] | None:
    """Return the first eligible terminal checkpoint, never using incomplete evidence."""
    day_14 = started + timedelta(days=minimum_days - 1)
    day_21 = started + timedelta(days=maximum_days - 1)
    latest = min(evaluated_local_date, day_21)
    if latest < day_14:
        return None
    for day_offset in range((day_14 - started).days, (latest - started).days + 1):
        cutoff = started + timedelta(days=day_offset)
        cutoff_attempts = [
            attempt
            for attempt in attempts
            if date.fromisoformat(str(attempt["local_date"])) <= cutoff
        ]
        metrics = _product_experiment_metrics(cutoff_attempts)
        if (
            not metrics["has_missing_evidence"]
            and int(metrics["complete_attempt_count"]) >= PRODUCT_EXPERIMENT_MIN_ATTEMPTS
        ):
            terminal_status = (
                "passed" if _product_value_gate_passed(metrics) else "not_passed"
            )
            return cutoff, terminal_status
    return None


def _persist_product_experiment_terminal(
    conn: sqlite3.Connection,
    *,
    experiment_ref: str,
    terminal_date: date,
    terminal_status: str,
    current: datetime | None = None,
) -> None:
    if terminal_status not in PRODUCT_EXPERIMENT_TERMINAL_STATUSES:
        raise ContractError("invalid_experiment_terminal_status")
    query_only_row = conn.execute("PRAGMA query_only").fetchone()
    if query_only_row and bool(query_only_row[0]):
        raise ContractError("experiment_finalize_requires_write_connection")
    had_transaction = conn.in_transaction
    try:
        conn.execute(
            "UPDATE context_product_experiments SET status=?,ended_local_date=?,updated_at=? "
            "WHERE experiment_ref=? AND status='running'",
            (
                terminal_status,
                terminal_date.isoformat(),
                _iso(_aware_utc(current)) if current is not None else _utc_now(),
                experiment_ref,
            ),
        )
        if not had_transaction:
            conn.commit()
    except sqlite3.Error as exc:
        if not had_transaction and conn.in_transaction:
            conn.rollback()
        raise ContractError("experiment_terminal_persist_failed") from exc


def _finalize_product_experiment_after_write_locked(
    conn: sqlite3.Connection,
    *,
    experiment_ref: str,
    evaluated_local_date: date,
    allow_insufficient: bool = False,
    current: datetime | None = None,
) -> tuple[date, str] | None:
    """Persist a terminal result from complete evidence in the caller's write transaction."""

    experiment = conn.execute(
        "SELECT started_local_date,status,minimum_days,maximum_days "
        "FROM context_product_experiments WHERE experiment_ref=?",
        (experiment_ref,),
    ).fetchone()
    if not experiment:
        raise ContractError("experiment_not_found")
    if str(experiment["status"] or "") != "running":
        raise ContractError("experiment_not_running")
    try:
        started = _canonical_product_experiment_date(
            experiment["started_local_date"],
            "invalid_experiment_date",
        )
        minimum = int(experiment["minimum_days"])
        maximum = int(experiment["maximum_days"])
        if (
            minimum != PRODUCT_EXPERIMENT_MIN_DAYS
            or maximum != PRODUCT_EXPERIMENT_MAX_DAYS
        ):
            raise ContractError("invalid_experiment_window")
    except (TypeError, ValueError) as exc:
        raise ContractError("invalid_experiment_date") from exc
    maximum_date = started + timedelta(days=maximum - 1)
    cutoff = min(evaluated_local_date, maximum_date)
    attempts = _product_experiment_attempt_rows(
        conn,
        experiment_ref,
        cutoff,
        started=started,
        maximum_date=maximum_date,
    )
    metrics = _product_experiment_metrics(attempts)
    if metrics["has_missing_evidence"]:
        if allow_insufficient:
            raise ContractError("experiment_evidence_incomplete")
        return None
    terminal = _find_product_experiment_terminal(
        attempts,
        started=started,
        minimum_days=minimum,
        maximum_days=maximum,
        evaluated_local_date=evaluated_local_date,
    )
    if (
        terminal is None
        and allow_insufficient
        and evaluated_local_date >= maximum_date
        and int(metrics["complete_attempt_count"]) < PRODUCT_EXPERIMENT_MIN_ATTEMPTS
    ):
        terminal = (maximum_date, "not_passed")
    if terminal is not None:
        _persist_product_experiment_terminal(
            conn,
            experiment_ref=experiment_ref,
            terminal_date=terminal[0],
            terminal_status=terminal[1],
            current=current,
        )
    return terminal


def _same_optional_float(stored_value: Any, incoming_value: float | None) -> bool:
    if stored_value is None or incoming_value is None:
        return stored_value is None and incoming_value is None
    return float(stored_value).hex() == incoming_value.hex()


def record_product_experiment_attempt(
    conn: sqlite3.Connection,
    *,
    experiment_ref: str,
    local_date: str,
    resume_seconds: float | None,
    event_id: str = "",
    useful: bool | None = None,
    source_ref: str = "",
    outcome: str | None = None,
) -> dict[str, Any]:
    """Record one content-free retrieval denominator event.

    ``event_id``/``useful`` are the canonical Group E contract.  The v2
    ``source_ref``/``outcome`` names remain accepted so an existing caller can
    migrate without changing idempotency or inventing missing evidence.
    """
    try:
        _validate_opaque_ref("experiment_ref", experiment_ref)
        attempt_date = date.fromisoformat(local_date)
        if event_id:
            _validate_opaque_ref("event_id", event_id)
        if source_ref:
            _validate_opaque_ref("source_ref", source_ref)
        if not event_id and not source_ref:
            raise ContractError("experiment_event_id_required")
        if outcome not in {None, "useful", "not_useful"}:
            raise ContractError("invalid_experiment_outcome")
        if useful is not None and not isinstance(useful, bool):
            raise ContractError("invalid_experiment_useful")
        outcome_useful = None if outcome is None else outcome == "useful"
        if useful is not None and outcome_useful is not None and useful != outcome_useful:
            raise ContractError("experiment_useful_outcome_conflict")
        normalized_useful = useful if useful is not None else outcome_useful
        normalized_outcome = (
            None if normalized_useful is None else ("useful" if normalized_useful else "not_useful")
        )
        if resume_seconds is not None:
            seconds = float(resume_seconds)
            if not math.isfinite(seconds) or seconds < 0 or seconds > 86_400:
                raise ContractError("invalid_resume_seconds")
        else:
            seconds = None
    except (ContractError, TypeError, ValueError) as exc:
        reason = exc.reason_code if isinstance(exc, ContractError) else "invalid_experiment_attempt"
        return _result("rejected", reason)

    normalized_event_id = event_id or source_ref
    normalized_source_ref = source_ref or _derived_ref(
        "source",
        f"product-experiment|{experiment_ref}|{normalized_event_id}",
    )
    now = _utc_now()
    attempt_ref = _derived_ref("attempt", f"{experiment_ref}|{normalized_event_id}")
    try:
        conn.execute("BEGIN IMMEDIATE")
        experiment = conn.execute(
            "SELECT started_local_date,ended_local_date,status,minimum_days,maximum_days,timezone_name "
            "FROM context_product_experiments WHERE experiment_ref=?",
            (experiment_ref,),
        ).fetchone()
        if not experiment:
            raise ContractError("experiment_not_found")
        started = _canonical_product_experiment_date(
            experiment["started_local_date"],
            "invalid_experiment_date",
        )
        _context_timezone_name(conn)
        snapshot_timezone = _experiment_timezone_name(experiment["timezone_name"])
        existing = conn.execute(
            "SELECT event_id,local_date,useful,outcome,resume_seconds,source_ref "
            "FROM context_product_experiment_attempts WHERE experiment_ref=? AND event_id=?",
            (experiment_ref, normalized_event_id),
        ).fetchone()
        if existing:
            existing_useful = _product_attempt_useful_value(existing)
            # Group E idempotency is keyed by event_id and its canonical
            # content-free payload.  source_ref is a v2 compatibility alias,
            # not an extra business field that can turn an otherwise exact
            # canonical replay into a false conflict.
            exact = (
                str(existing["local_date"]) == attempt_date.isoformat()
                and existing_useful is normalized_useful
                and _same_optional_float(existing["resume_seconds"], seconds)
            )
            if not exact:
                raise ContractError("experiment_attempt_conflict")
            conn.commit()
            return {
                "status": "replayed",
                "reason_code": "idempotent_replay",
                "attempt_ref": attempt_ref,
                "experiment_ref": experiment_ref,
                "event_id": normalized_event_id,
                "local_date": attempt_date.isoformat(),
                "useful": normalized_useful,
                "resume_seconds": seconds,
                "raw_access": False,
            }
        stored_status = str(experiment["status"] or "")
        if stored_status != "running":
            raise ContractError("experiment_not_running")
        if attempt_date < started:
            raise ContractError("attempt_before_experiment")
        maximum_attempt_date = started + timedelta(days=int(experiment["maximum_days"]) - 1)
        if attempt_date > maximum_attempt_date:
            raise ContractError("attempt_after_experiment_window")
        current_local_date = _local_today(conn, timezone_name=snapshot_timezone)
        if attempt_date > current_local_date:
            raise ContractError("attempt_after_current_local_date")
        existing_attempts = _product_experiment_attempt_rows(
            conn,
            experiment_ref,
            min(current_local_date, maximum_attempt_date),
            started=started,
            maximum_date=maximum_attempt_date,
        )
        terminal = _find_product_experiment_terminal(
            existing_attempts,
            started=started,
            minimum_days=int(experiment["minimum_days"]),
            maximum_days=int(experiment["maximum_days"]),
            evaluated_local_date=current_local_date,
        )
        if terminal is not None:
            terminal_date, terminal_status = terminal
            _persist_product_experiment_terminal(
                conn,
                experiment_ref=experiment_ref,
                terminal_date=terminal_date,
                terminal_status=terminal_status,
            )
            conn.commit()
            raise ContractError("experiment_not_running")
        conn.execute(
            "INSERT INTO context_product_experiment_attempts(attempt_ref,experiment_ref,event_id,local_date,useful,"
            "outcome,resume_seconds,source_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                attempt_ref,
                experiment_ref,
                normalized_event_id,
                attempt_date.isoformat(),
                None if normalized_useful is None else int(normalized_useful),
                normalized_outcome,
                seconds,
                normalized_source_ref,
                now,
            ),
        )
        _finalize_product_experiment_after_write_locked(
            conn,
            experiment_ref=experiment_ref,
            evaluated_local_date=attempt_date,
        )
        conn.commit()
        return {
            "status": "recorded",
            "attempt_ref": attempt_ref,
            "experiment_ref": experiment_ref,
            "event_id": normalized_event_id,
            "local_date": attempt_date.isoformat(),
            "useful": normalized_useful,
            "outcome": normalized_outcome,
            "resume_seconds": seconds,
            "source_ref": normalized_source_ref,
            "raw_access": False,
        }
    except ContractError as exc:
        if conn.in_transaction:
            conn.rollback()
        status = "conflict" if exc.reason_code == "experiment_attempt_conflict" else "rejected"
        return _result(status, exc.reason_code)
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "database_write_failed")


def finalize_product_experiment(
    conn: sqlite3.Connection,
    *,
    experiment_ref: str,
    confirmation: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Explicitly materialize a terminal experiment result; never called by a read path."""

    try:
        _validate_opaque_ref("experiment_ref", experiment_ref)
        if confirmation != PRODUCT_EXPERIMENT_FINALIZE_CONFIRMATION:
            raise ContractError("experiment_finalize_confirmation_required")
    except ContractError as exc:
        return _result("rejected", exc.reason_code)
    # As with experiment start, production time is sampled only after the write
    # transaction and both current/persisted timezone boundaries are known.
    # An injected timestamp remains fixed for deterministic replay and tests.
    injected_current = _aware_utc(now) if now is not None else None
    evaluated_local_date: date | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        # The mutable configured timezone is part of the current trust
        # boundary. Validate it inside the same transaction before any
        # terminal state can be persisted from the experiment snapshot.
        if not context_console_enabled(conn):
            raise ContractError("feature_flag_off")
        _context_timezone_name(conn)
        experiment = conn.execute(
            "SELECT status,timezone_name FROM context_product_experiments WHERE experiment_ref=?",
            (experiment_ref,),
        ).fetchone()
        if not experiment:
            raise ContractError("experiment_not_found")
        stored_status = str(experiment["status"] or "")
        timezone_name = _experiment_timezone_name(experiment["timezone_name"])
        current = injected_current if injected_current is not None else _aware_utc(None)
        evaluated_local_date = _local_today(
            conn,
            now=current,
            timezone_name=timezone_name,
        )
        if stored_status in PRODUCT_EXPERIMENT_TERMINAL_STATUSES:
            conn.rollback()
            payload = product_experiment_status(
                conn,
                experiment_ref=experiment_ref,
                local_today=evaluated_local_date,
            )
            payload["finalization_status"] = "replayed"
            return payload
        if stored_status != "running":
            raise ContractError("experiment_not_running")
        terminal = _finalize_product_experiment_after_write_locked(
            conn,
            experiment_ref=experiment_ref,
            evaluated_local_date=evaluated_local_date,
            allow_insufficient=True,
            current=current,
        )
        if terminal is None:
            raise ContractError("experiment_not_ready_to_finalize")
        conn.commit()
    except ContractError as exc:
        if conn.in_transaction:
            conn.rollback()
        status = "disabled" if exc.reason_code == "feature_flag_off" else "rejected"
        return _result(status, exc.reason_code)
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "database_write_failed")
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    payload = product_experiment_status(
        conn,
        experiment_ref=experiment_ref,
        local_today=evaluated_local_date,
    )
    payload["finalization_status"] = "finalized"
    return payload


def product_experiment_status(
    conn: sqlite3.Connection,
    *,
    experiment_ref: str = "",
    local_today: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        # The configured policy is part of the current trust boundary even
        # when an experiment has its own immutable timezone snapshot.
        _context_timezone_name(conn)
    except ContractError as exc:
        return _unknown_product_experiment_status(
            exc.reason_code,
            experiment_ref=experiment_ref,
        )
    if experiment_ref:
        row = conn.execute(
            "SELECT * FROM context_product_experiments WHERE experiment_ref=?", (experiment_ref,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM context_product_experiments "
            "ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END,created_at DESC LIMIT 1"
        ).fetchone()
    thresholds = _product_experiment_thresholds()
    if not row:
        return {
            "status": "not_started",
            "experiment_started": False,
            "minimum_days": PRODUCT_EXPERIMENT_MIN_DAYS,
            "maximum_days": PRODUCT_EXPERIMENT_MAX_DAYS,
            "attempt_count": 0,
            "rated_attempt_count": 0,
            "timed_attempt_count": 0,
            "complete_attempt_count": 0,
            "missing_useful_count": 0,
            "missing_resume_count": 0,
            "useful_count": None,
            "not_useful_count": None,
            "useful_rate": None,
            "median_resume_seconds": None,
            "evidence_status": "empty",
            "review_readiness": "pending",
            "completion_readiness": "pending",
            "decision_status": "pending",
            "value_gate_status": "pending",
            "value_gate_passed": None,
            "thresholds": thresholds,
            "raw_access": False,
        }

    try:
        started = _canonical_product_experiment_date(
            row["started_local_date"],
            "invalid_experiment_date",
        )
        timezone_name = _experiment_timezone_name(row["timezone_name"])
        if local_today is not None and (
            not isinstance(local_today, date) or isinstance(local_today, datetime)
        ):
            raise ContractError("invalid_experiment_date")
        today = local_today or _local_today(conn, now=now, timezone_name=timezone_name)
        minimum = int(row["minimum_days"])
        maximum = int(row["maximum_days"])
        if (
            minimum != PRODUCT_EXPERIMENT_MIN_DAYS
            or maximum != PRODUCT_EXPERIMENT_MAX_DAYS
        ):
            raise ContractError("invalid_experiment_window")
    except (ContractError, TypeError, ValueError) as exc:
        reason = exc.reason_code if isinstance(exc, ContractError) else "invalid_experiment_date"
        return _unknown_product_experiment_status(
            reason,
            experiment_ref=str(row["experiment_ref"]),
        )

    if today < started:
        return _unknown_product_experiment_status(
            "experiment_before_start",
            experiment_ref=str(row["experiment_ref"]),
        )

    elapsed = (today - started).days + 1
    maximum_date = started + timedelta(days=maximum - 1)
    initial_cutoff = min(today, maximum_date)
    try:
        all_attempts = _product_experiment_attempt_rows(
            conn,
            str(row["experiment_ref"]),
            initial_cutoff,
            started=started,
            maximum_date=maximum_date,
        )
    except (ContractError, TypeError, ValueError):
        return _unknown_product_experiment_status(
            "invalid_experiment_attempt_evidence",
            experiment_ref=str(row["experiment_ref"]),
        )
    stored_status = str(row["status"] or "")
    terminal: tuple[date, str] | None = None
    if stored_status in PRODUCT_EXPERIMENT_TERMINAL_STATUSES:
        try:
            if not row["ended_local_date"]:
                raise ContractError("experiment_terminal_date_missing")
            ended_local_date = _canonical_product_experiment_date(
                row["ended_local_date"],
                "invalid_experiment_terminal_date",
            )
            if ended_local_date < started or ended_local_date > maximum_date:
                raise ContractError("invalid_experiment_terminal_date")
            terminal = (ended_local_date, stored_status)
        except (ContractError, TypeError, ValueError) as exc:
            reason = (
                exc.reason_code
                if isinstance(exc, ContractError)
                else "invalid_experiment_terminal_date"
            )
            return _unknown_product_experiment_status(
                reason,
                experiment_ref=str(row["experiment_ref"]),
            )
    elif stored_status == "running":
        try:
            terminal = _find_product_experiment_terminal(
                all_attempts,
                started=started,
                minimum_days=minimum,
                maximum_days=maximum,
                evaluated_local_date=today,
            )
        except (ContractError, TypeError, ValueError):
            return _unknown_product_experiment_status(
                "invalid_experiment_attempt_evidence",
                experiment_ref=str(row["experiment_ref"]),
            )
    else:
        return _unknown_product_experiment_status(
            "invalid_experiment_status",
            experiment_ref=str(row["experiment_ref"]),
        )

    evidence_cutoff = terminal[0] if terminal is not None else initial_cutoff
    if evidence_cutoff > initial_cutoff:
        try:
            all_attempts = _product_experiment_attempt_rows(
                conn,
                str(row["experiment_ref"]),
                evidence_cutoff,
                started=started,
                maximum_date=maximum_date,
            )
        except (ContractError, TypeError, ValueError):
            return _unknown_product_experiment_status(
                "invalid_experiment_attempt_evidence",
                experiment_ref=str(row["experiment_ref"]),
            )
    attempts = [
        attempt
        for attempt in all_attempts
        if date.fromisoformat(str(attempt["local_date"])) <= evidence_cutoff
    ]
    metrics = _product_experiment_metrics(attempts)
    attempt_count = int(metrics["attempt_count"])
    complete_count = int(metrics["complete_attempt_count"])
    has_missing_evidence = bool(metrics["has_missing_evidence"])
    day14 = elapsed >= minimum or terminal is not None
    day21 = elapsed >= maximum
    # Day 21 is inclusive: a tenth complete attempt recorded anywhere on that
    # natural day may still freeze the experiment. Automatic insufficiency is
    # therefore a post-window (Day 22) read state; an explicitly confirmed
    # finalize may still materialize insufficiency during Day 21.
    maximum_window_closed = today > maximum_date
    minimum_attempts_met = complete_count >= PRODUCT_EXPERIMENT_MIN_ATTEMPTS
    metrics_evaluable = bool(
        day14
        and minimum_attempts_met
        and metrics["useful_rate"] is not None
        and metrics["median_resume_seconds"] is not None
    )
    value_gate_passed = _product_value_gate_passed(metrics) if metrics_evaluable else None

    reason_code = ""
    extension_status = "not_applicable"
    decision_status = "pending"
    if terminal is not None:
        if terminal[1] == "not_passed" and not minimum_attempts_met:
            phase = "insufficient"
            decision_status = "not_passed"
            reason_code = "insufficient_complete_attempts_at_day_21"
            extension_status = "maximum_window_reached"
        else:
            phase = terminal[1]
            decision_status = terminal[1]
            reason_code = (
                "value_gate_passed"
                if terminal[1] == "passed"
                else "value_gate_threshold_not_met"
            )
    elif not day14:
        phase = "collecting"
        reason_code = "minimum_window_not_reached"
    elif has_missing_evidence:
        phase = "incomplete"
        reason_code = "missing_attempt_evidence"
        extension_status = "window_elapsed_incomplete" if maximum_window_closed else "not_applicable"
    elif not minimum_attempts_met:
        if maximum_window_closed:
            phase = "insufficient"
            reason_code = "insufficient_complete_attempts_at_day_21"
            decision_status = "not_passed"
            extension_status = "maximum_window_reached"
        else:
            phase = "extended"
            reason_code = "insufficient_complete_attempts_at_day_14"
            extension_status = "day_21_in_progress" if day21 else "extended_to_day_21"
    else:
        # This branch is unreachable for valid rows because a complete
        # evaluable set becomes a terminal above. Keep it fail closed if a
        # future schema violates that invariant.
        phase = "unknown"
        reason_code = "experiment_terminal_not_materialized"

    return {
        "status": phase,
        "reason_code": reason_code,
        "experiment_started": True,
        "experiment_ref": str(row["experiment_ref"]),
        "started_local_date": started.isoformat(),
        "evaluated_local_date": today.isoformat(),
        "evidence_cutoff_local_date": evidence_cutoff.isoformat(),
        "timezone_name": timezone_name,
        "local_day_semantics": "iana_calendar_day",
        "elapsed_natural_days": elapsed,
        "minimum_days": minimum,
        "maximum_days": maximum,
        "attempt_count": attempt_count,
        "rated_attempt_count": metrics["rated_attempt_count"],
        "timed_attempt_count": metrics["timed_attempt_count"],
        "complete_attempt_count": complete_count,
        "missing_useful_count": metrics["missing_useful_count"],
        "missing_resume_count": metrics["missing_resume_count"],
        "useful_count": metrics["useful_count"],
        "not_useful_count": metrics["not_useful_count"],
        "useful_rate": metrics["useful_rate"],
        "median_resume_seconds": metrics["median_resume_seconds"],
        "evidence_status": metrics["evidence_status"],
        "thresholds": thresholds,
        "checkpoints": {
            "day_14_reached": day14,
            "day_21_reached": day21,
            "attempt_9_reached": attempt_count >= 9,
            "attempt_10_reached": attempt_count >= PRODUCT_EXPERIMENT_MIN_ATTEMPTS,
            "complete_attempt_10_reached": minimum_attempts_met,
        },
        "extension_status": extension_status,
        "review_readiness": "ready" if metrics_evaluable else "pending",
        "completion_readiness": decision_status,
        "decision_status": decision_status,
        "value_gate_status": decision_status,
        "value_gate_passed": value_gate_passed,
        "raw_access": False,
    }

def record_component_baseline(
    conn: sqlite3.Connection,
    *,
    component: str,
    version_ref: str,
    reset_reason_code: str,
) -> dict[str, Any]:
    try:
        if component not in STABILITY_COMPONENTS:
            raise ContractError("invalid_stability_component")
        _validate_opaque_ref("version_ref", version_ref)
        _validate_reason_code(reset_reason_code)
    except ContractError as exc:
        return _result("rejected", exc.reason_code)
    now = _utc_now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO context_component_baselines(component,version_ref,generation,reset_reason_code,recorded_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(component) DO UPDATE SET version_ref=excluded.version_ref,"
            "generation=context_component_baselines.generation+1,"
            "reset_reason_code=excluded.reset_reason_code,recorded_at=excluded.recorded_at",
            (component, version_ref, 1, reset_reason_code, now),
        )
        row = conn.execute(
            "SELECT generation,recorded_at FROM context_component_baselines WHERE component=?",
            (component,),
        ).fetchone()
        generation = int(row["generation"])
        conn.commit()
        return {
            "status": "baseline_recorded",
            "component": component,
            "generation": generation,
            "recorded_at": str(row["recorded_at"]),
            "product_experiment_reset": False,
            "product_wins_reset": False,
            "raw_access": False,
        }
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _result("failed", "component_baseline_write_failed")


def classify_stability_change(change_kind: str) -> dict[str, Any]:
    if change_kind in NO_RESET_CHANGE_KINDS:
        return {
            "status": "no_reset",
            "change_kind": change_kind,
            "affected_components": [],
            "product_experiment_reset": False,
            "raw_access": False,
        }
    if change_kind in STABILITY_COMPONENTS:
        return {
            "status": "component_reset_required",
            "change_kind": change_kind,
            "affected_components": [change_kind],
            "product_experiment_reset": False,
            "raw_access": False,
        }
    return _result("rejected", "invalid_change_kind")


def runtime_stability_status(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    days: int = 7,
) -> dict[str, Any]:
    from .stability import component_baseline_windows

    end_at = _aware_utc(now)
    window_days = max(1, min(int(days), 31))
    windows = component_baseline_windows(
        conn,
        end_at - timedelta(days=window_days),
        end_at,
        window_days,
    )
    return {
        "status": "available",
        "components": [
            {
                "component": component,
                **{
                    key: value
                    for key, value in windows[component].items()
                    if key != "from_dt"
                },
            }
            for component in sorted(STABILITY_COMPONENTS)
        ],
        "window_days": window_days,
        "observed_at": _iso(end_at),
        "raw_access": False,
    }


def context_console_status(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    current = _aware_utc(now)
    observed_at = _iso(current)
    local_day_policy = context_local_timezone_status(conn)
    product_experiment = product_experiment_status(conn, now=current)
    if (
        local_day_policy["status"] == "unknown"
        or product_experiment["status"] == "unknown"
    ):
        reason_code = (
            str(local_day_policy["reason_code"])
            if local_day_policy["status"] == "unknown"
            else str(product_experiment["reason_code"])
        )
        unknown = _unknown_status(reason_code, observed_at)
        unknown.update(
            {
                "local_day_policy": local_day_policy,
                "product_wins": {
                    "status": "unknown",
                    "reason_code": reason_code,
                    "active_win_count": None,
                    "minutes_saved": None,
                    "raw_access": False,
                },
                "product_experiment": product_experiment,
                "runtime_stability": {
                    "status": "unknown",
                    "reason_code": reason_code,
                    "components": None,
                    "observed_at": observed_at,
                    "raw_access": False,
                },
            }
        )
        return unknown
    enabled = context_console_enabled(conn)
    if not enabled:
        return {
            "status": "disabled",
            "reason_code": "feature_flag_off",
            "observed_at": observed_at,
            "last_successful_read_at": observed_at,
            "raw_access": False,
            "service_status": "disabled",
            "actions_enabled": False,
            "feature_flag_enabled": False,
            "memory_writer_enabled": False,
            "source_revocation_cascade_ready": False,
            "local_day_policy": local_day_policy,
            "product_experiment": product_experiment,
            "runtime_stability": runtime_stability_status(conn, now=current),
        }
    counts = {
        "active_verified_contexts": _count_valid_verified_contexts(conn, current),
        "active_unverified_clues": _count_valid_unverified_clues(conn, current),
        "memory_candidates": _count_valid_memory_candidates(conn, current),
    }
    return {
        "status": "available",
        "reason_code": "",
        "observed_at": observed_at,
        "last_successful_read_at": observed_at,
        "raw_access": False,
        "service_status": "available",
        "actions_enabled": True,
        "feature_flag_enabled": True,
        "memory_writer_enabled": False,
        "source_revocation_cascade_ready": False,
        "counts": counts,
        "local_day_policy": local_day_policy,
        "product_wins": product_win_status(conn, now=current),
        "product_experiment": product_experiment,
        "runtime_stability": runtime_stability_status(conn, now=current),
    }


def context_console_status_path(
    db_path: str | Path,
    *,
    busy_timeout_seconds: float = 0.05,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_at = _iso(_aware_utc(now))
    path = Path(db_path).expanduser().resolve()
    if not path.exists():
        return _unknown_status("database_missing", observed_at)
    try:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=max(0.0, float(busy_timeout_seconds)),
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={max(0, int(float(busy_timeout_seconds) * 1000))}")
        conn.execute("PRAGMA query_only=ON")
        try:
            # Force a schema and table read; do not infer health from an empty or
            # cached success object.
            conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            return context_console_status(conn, now=now)
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        reason = "database_busy" if "locked" in message or "busy" in message else "database_unavailable"
        return _unknown_status(reason, observed_at)
    except ContractError as exc:
        return _unknown_status(exc.reason_code, observed_at)
    except (sqlite3.Error, TypeError, ValueError):
        return _unknown_status("database_unavailable", observed_at)


def _phase2_unknown(contract_version: str, reason_code: str, observed_at: str) -> dict[str, Any]:
    public_status = {
        "database_busy": "busy",
        "database_missing": "unavailable",
        "database_unavailable": "unavailable",
    }.get(reason_code, "unknown")
    common: dict[str, Any] = {
        "contract_version": contract_version,
        "status": public_status,
        "observed_at": observed_at,
        "last_successful_read_at": None,
        "actions_enabled": False,
        "raw_access": False,
    }
    if contract_version == CONTEXT_SNAPSHOT_VERSION:
        common.update(
            {
                "counts": None,
                "context_bundle_v1": None,
                "projects": None,
                "legacy_confirmed": None,
                "confirmation_questions": None,
                "capabilities": None,
            }
        )
    elif contract_version == MEMORY_CANDIDATE_LIST_VERSION:
        common.update({"candidates": None, "actions": None})
    elif contract_version == DELIVERY_STATUS_VERSION:
        common.update({"daily_report": None, "deliveries": None})
    elif contract_version == LOCAL_HANDOFF_VERSION:
        common.update(
            {
                "handoff_ref": None,
                "bundle_hash": None,
                "project": None,
                "verified_contexts": None,
                "unresolved_questions": None,
                "expires_at": None,
                "revocation_state": None,
            }
        )
    elif contract_version == PRODUCT_EXPERIMENT_STATUS_VERSION:
        return _public_product_experiment_unknown(
            public_status,
            now=_parse_contract_time(observed_at),
        )
    return common


def phase2_unknown_contract(
    contract_version: str,
    reason_code: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a public fail-closed envelope without opening a database."""
    return _phase2_unknown(
        contract_version,
        reason_code,
        _iso(_aware_utc(now)),
    )


def _phase2_read_payload(
    raw_payload: bytes | str,
    allowed_fields: set[str],
) -> dict[str, Any]:
    return _parse_closed_json(
        raw_payload,
        allowed_fields,
        required_fields=set(),
    )


def _phase2_limit(value: Any, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ContractError("invalid_limit")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError("invalid_limit") from exc
    if normalized < 1 or normalized > maximum:
        raise ContractError("invalid_limit")
    return normalized


def _phase2_time_range(
    conn: sqlite3.Connection,
    *,
    start_at: Any = "",
    end_at: Any = "",
    query: str = "",
    now: datetime | None = None,
) -> tuple[str, str]:
    start_text = str(start_at or "")
    end_text = str(end_at or "")
    if bool(start_text) != bool(end_text):
        raise ContractError("complete_time_range_required")
    if start_text and end_text:
        start = _parse_contract_time(start_text)
        end = _parse_contract_time(end_text)
        if start >= end:
            raise ContractError("invalid_time_range")
        return _iso(start), _iso(end)
    current = _aware_utc(now)
    zone = _timezone_for_name(_context_timezone_name(conn))
    local_now = current.astimezone(zone)
    local_start = datetime.combine(local_now.date(), time.min, tzinfo=zone)
    if query:
        local_start -= timedelta(days=VERIFIED_RETENTION_DAYS - 1)
    local_end = datetime.combine(
        local_now.date() + timedelta(days=1),
        time.min,
        tzinfo=zone,
    )
    return _iso(local_start.astimezone(timezone.utc)), _iso(local_end.astimezone(timezone.utc))


def _legacy_context_state(activity: Mapping[str, Any]) -> str:
    outcome = str(activity.get("outcome") or "unknown")
    event_type = str(activity.get("event_type") or "")
    if outcome == "blocked":
        return "blocked"
    if outcome == "completed" or event_type in {
        "completion",
        "submission",
        "send",
        "publish",
        "purchase",
    }:
        return "completed_confirmed"
    if outcome == "preparation_only":
        return "intent_observed"
    return "in_progress"


def _legacy_safe_text(value: Any, *, maximum: int) -> str:
    from .user_decisions import safe_decision_text

    text = safe_decision_text(value or "", max_len=maximum)
    if (
        not text
        or _PATH_OR_URL_RE.search(text)
        or _SECRET_LIKE_RE.search(text)
        or _contains_unicode_control(text)
        or _looks_like_json_document(text)
    ):
        return ""
    return text


def _legacy_fts_matching_item_keys(
    rows: Sequence[sqlite3.Row],
    *,
    query: str,
    limit: int,
) -> set[str]:
    """Search bounded legacy safe summaries without persisting a new index.

    Schema 31 does not rewrite historical activity. A small in-memory FTS5
    index keeps the compatibility adapter FTS-only while allowing old manual
    confirmations (which have no source activity row) to remain findable until
    their natural retention expiry.
    """
    if not query:
        return {str(row["item_key"]) for row in rows}
    search_db = sqlite3.connect(":memory:")
    try:
        search_db.execute(
            "CREATE VIRTUAL TABLE legacy_confirmation_fts USING fts5("
            "item_key UNINDEXED,summary,workstream)"
        )
        values: list[tuple[str, str, str]] = []
        for row in rows:
            summary = _legacy_safe_text(
                row["cleaned_content"] or row["safe_observation"] or "",
                maximum=240,
            )
            workstream = _legacy_safe_text(
                row["user_workstream"] or row["suggested_workstream"] or "",
                maximum=120,
            )
            if summary:
                values.append((str(row["item_key"]), summary, workstream))
        search_db.executemany(
            "INSERT INTO legacy_confirmation_fts(item_key,summary,workstream) VALUES(?,?,?)",
            values,
        )
        return {
            str(row[0])
            for row in search_db.execute(
                "SELECT item_key FROM legacy_confirmation_fts "
                "WHERE legacy_confirmation_fts MATCH ? LIMIT ?",
                (_fts_query(query), limit),
            ).fetchall()
        }
    except sqlite3.Error:
        return set()
    finally:
        search_db.close()


def _legacy_confirmed_projection(
    conn: sqlite3.Connection,
    *,
    project_ref: str,
    start_at: str,
    end_at: str,
    query: str,
    limit: int,
    now: datetime,
) -> list[dict[str, Any]]:
    from .activity_confirmations import _confirmation_source_is_live

    candidate_limit = max(50, min(limit * 4, 800))
    rows = conn.execute(
        "SELECT item.* FROM activity_confirmation_items AS item "
        "WHERE item.state='confirmed' AND item.answer='occurred' "
        "AND datetime(item.observed_start)>=datetime(?) "
        "AND datetime(item.observed_start)<datetime(?) "
        "AND NOT EXISTS ("
        "SELECT 1 FROM context_activity_confirmation_links AS manual_link "
        "WHERE manual_link.confirmation_kind='manual_note' "
        "AND manual_link.confirmation_key=item.item_key"
        ") "
        "AND NOT EXISTS ("
        "SELECT 1 FROM context_activity_confirmation_links AS card_link "
        "WHERE card_link.confirmation_kind='confirm_card' "
        "AND card_link.confirmation_key="
        "COALESCE(NULLIF(item.group_key,''),item.item_key)"
        ") "
        "ORDER BY datetime(item.observed_start) DESC,item.item_key LIMIT ?",
        (start_at, end_at, candidate_limit),
    ).fetchall()
    matching_item_keys = _legacy_fts_matching_item_keys(
        rows,
        query=query,
        limit=candidate_limit,
    )
    if query and not matching_item_keys:
        return []
    output: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for row in rows:
        if not _confirmation_source_is_live(conn, row):
            continue
        activity = dict(row)
        if str(activity.get("item_key") or "") not in matching_item_keys:
            continue
        group_key = str(activity.get("group_key") or activity.get("item_key") or "")
        if group_key in seen_groups:
            continue
        summary = _legacy_safe_text(
            activity.get("cleaned_content")
            or activity.get("safe_observation")
            or "",
            maximum=240,
        )
        workstream = _legacy_safe_text(
            activity.get("user_workstream")
            or activity.get("suggested_workstream")
            or "",
            maximum=120,
        )
        if not summary:
            continue
        legacy_project_ref = _derived_ref(
            "project",
            f"legacy|{workstream or 'unclassified'}",
        )
        if project_ref and legacy_project_ref != project_ref:
            continue
        state = _legacy_context_state(activity)
        occurred = _parse_contract_time(str(activity.get("observed_start") or ""))
        expires_at = _verified_expiry(conn, occurred)
        if _parse_contract_time(expires_at) <= now:
            continue
        public_projection: dict[str, Any] = {
            "state": state,
            "occurred_at": _iso(occurred),
            "expires_at": expires_at,
            "revocation_state": "active",
            "privacy_class": "private_local",
            "project": {
                "ref": legacy_project_ref,
                "label": workstream or "未归类",
            },
            "summary": summary,
            "source": {
                "kind": "user_confirmation",
                "label": "用户确认",
            },
            "compatibility_source": True,
            "external_delivery_eligible": False,
            "memory_candidate_eligible": False,
            "raw_access": False,
        }
        public_projection["context_ref"] = _derived_ref(
            "context",
            json.dumps(
                public_projection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        output.append(public_projection)
        seen_groups.add(group_key)
        if len(output) >= limit:
            break
    output.sort(
        key=lambda item: (
            str(item["occurred_at"]),
            str(item["context_ref"]),
        ),
        reverse=True,
    )
    return output[:limit]


def _confirmation_questions(
    conn: sqlite3.Connection,
    *,
    start_at: str,
    end_at: str,
    limit: int = 50,
    project_ref: str = "",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    from .activity_confirmations import list_activity_confirmation_cards

    cards = list_activity_confirmation_cards(
        conn,
        range_name="today",
        start=start_at,
        end=end_at,
        limit=max(1, min(limit, 50)),
        scan_limit=PHASE2_CONFIRMATION_SCAN_LIMIT,
    )
    output: list[dict[str, Any]] = []
    for card in list(cards.get("cards") or []):
        if str(card.get("section") or "") != "pending_confirmation":
            continue
        revision = str(card.get("member_revision") or "")
        private_group_key = str(card.get("group_key") or "")
        confirmation_ref = _derived_ref(
            "confirmation",
            f"{private_group_key}|{revision}",
        )
        if project_ref:
            try:
                derived = _derive_confirmation_context_locked(
                    conn,
                    card=card,
                    confirmation_ref=confirmation_ref,
                    current=_aware_utc(now),
                    register_labels=False,
                )
            except ContractError:
                continue
            if str(derived.get("project_ref") or "") != project_ref:
                continue
        output.append(
            {
                "confirmation_ref": confirmation_ref,
                "revision": revision,
                "title": str(card.get("display_title") or ""),
                "question": str(card.get("display_question") or ""),
                "evidence": str(card.get("display_evidence") or ""),
                "priority": str(card.get("priority") or "normal"),
                "state": str(card.get("state") or "pending"),
                "event_type": str(card.get("event_type") or ""),
                "app": str(card.get("app") or ""),
                "time_label": str(card.get("time_label") or ""),
                "observed_start": str(card.get("observed_start") or ""),
                "observed_end": str(card.get("observed_end") or ""),
                "answer_options": list(card.get("answer_options") or []),
                "verification_state": "unverified",
                "raw_access": False,
            }
        )
        if len(output) >= 50:
            break
    return output


def _group_snapshot_projects(
    verified_contexts: Sequence[Mapping[str, Any]],
    legacy_confirmed: Sequence[Mapping[str, Any]],
    unverified_clues: Sequence[Mapping[str, Any]],
    *,
    maximum_projects: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item, member_kind in (
        *((item, "verified_contexts") for item in verified_contexts),
        *((item, "compatibility_contexts") for item in legacy_confirmed),
        *((item, "unverified_clues") for item in unverified_clues),
    ):
        project = dict(item.get("project") or {})
        key = str(project.get("ref") or project.get("label") or "project_unclassified")
        group = grouped.setdefault(
            key,
            {
                "project": project,
                "verified_contexts": [],
                "compatibility_contexts": [],
                "unverified_clues": [],
                "latest_occurred_at": "",
            },
        )
        group[member_kind].append(dict(item))
        group["latest_occurred_at"] = max(
            str(group["latest_occurred_at"]),
            str(item.get("occurred_at") or item.get("observed_at") or ""),
        )
    projects = sorted(
        grouped.values(),
        key=lambda value: (
            str(value["latest_occurred_at"]),
            str(value["project"].get("label") or ""),
        ),
        reverse=True,
    )[: max(1, min(maximum_projects, 50))]
    state_priority = {
        "completed_confirmed": 5,
        "external_delivered": 5,
        "artifact_committed": 5,
        "artifact_saved": 4,
        "blocked": 4,
        "in_progress": 3,
        "intent_observed": 2,
    }
    for project in projects:
        for key in ("verified_contexts", "compatibility_contexts"):
            project[key] = sorted(
                project[key],
                key=lambda item: (
                    str(item.get("occurred_at") or ""),
                    state_priority.get(str(item.get("state") or ""), 0),
                    str(item.get("context_ref") or item.get("source_ref") or ""),
                ),
                reverse=True,
            )[:20]
        project["unverified_clues"] = sorted(
            project["unverified_clues"],
            key=lambda item: (
                str(item.get("observed_at") or ""),
                str(item.get("source_ref") or ""),
            ),
            reverse=True,
        )[:20]
    return projects


def context_snapshot(
    conn: sqlite3.Connection,
    raw_payload: bytes | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    # An injected timestamp is an explicit deterministic contract for tests and
    # controlled replay. Production reads must refresh their trust timestamp
    # only after acquiring the authorization lease; otherwise an expiry or
    # local-midnight boundary crossed while waiting can leak stale trusted
    # content or use yesterday's default range.
    injected_current = _aware_utc(now) if now is not None else None
    current = injected_current if injected_current is not None else _aware_utc(None)
    observed_at = _iso(current)
    try:
        payload = _phase2_read_payload(raw_payload, _CONTEXT_SNAPSHOT_FIELDS)
        project_ref = str(payload.get("project_ref") or "")
        query = _validate_search_query(payload.get("query") or "")
        include_clues = payload.get("include_clues", True)
        if not isinstance(include_clues, bool):
            raise ContractError("include_clues_must_be_boolean")
        limit = _phase2_limit(payload.get("limit"), default=100, maximum=200)
        with _context_authorization_lock(conn, exclusive=False):
            current = injected_current if injected_current is not None else _aware_utc(None)
            observed_at = _iso(current)
            if not context_console_enabled(conn):
                return {
                    "contract_version": CONTEXT_SNAPSHOT_VERSION,
                    "status": "disabled",
                    "observed_at": observed_at,
                    "last_successful_read_at": observed_at,
                    "actions_enabled": False,
                    "counts": None,
                    "context_bundle_v1": None,
                    "projects": None,
                    "legacy_confirmed": None,
                    "confirmation_questions": None,
                    "capabilities": None,
                    "raw_access": False,
                }
            start_at, end_at = _phase2_time_range(
                conn,
                start_at=payload.get("start_at"),
                end_at=payload.get("end_at"),
                query=query,
                now=current,
            )
            legacy_only_project = False
            if project_ref:
                try:
                    _require_active_label(conn, project_ref, "project")
                except ContractError:
                    legacy_probe = _legacy_confirmed_projection(
                        conn,
                        project_ref=project_ref,
                        start_at=start_at,
                        end_at=end_at,
                        query="",
                        limit=1,
                        now=current,
                    )
                    if not legacy_probe:
                        raise
                    legacy_only_project = True
            bundle = _build_context_bundle_locked(
                conn,
                project_ref=project_ref,
                start_at=start_at,
                end_at=end_at,
                query=query,
                include_clues=include_clues,
                allow_unregistered_empty_project=legacy_only_project,
                limit=limit,
                now=current,
            )
            if bundle.get("status") != "available":
                return {
                    "contract_version": CONTEXT_SNAPSHOT_VERSION,
                    "status": str(bundle.get("status") or "rejected"),
                    "observed_at": observed_at,
                    "last_successful_read_at": observed_at,
                    "actions_enabled": False,
                    "counts": None,
                    "context_bundle_v1": None,
                    "projects": None,
                    "legacy_confirmed": None,
                    "confirmation_questions": None,
                    "capabilities": None,
                    "raw_access": False,
                }
            legacy = _legacy_confirmed_projection(
                conn,
                project_ref=project_ref,
                start_at=start_at,
                end_at=end_at,
                query=query,
                limit=limit,
                now=current,
            )
            questions = _confirmation_questions(
                conn,
                start_at=start_at,
                end_at=end_at,
                project_ref=project_ref,
                now=current,
            )
        projects = _group_snapshot_projects(
            list(bundle.get("verified_contexts") or []),
            legacy,
            list(bundle.get("unverified_clues") or []),
            maximum_projects=(
                min(limit, 50)
                if query or bool(payload.get("start_at") and payload.get("end_at"))
                else 5
            ),
        )
        return {
            "contract_version": CONTEXT_SNAPSHOT_VERSION,
            "status": "available",
            "observed_at": observed_at,
            "last_successful_read_at": observed_at,
            "actions_enabled": True,
            "counts": {
                "verified_contexts": len(bundle.get("verified_contexts") or []),
                "unverified_clues": len(bundle.get("unverified_clues") or []),
                "legacy_confirmed": len(legacy),
                "pending_confirmations": len(questions),
            },
            "context_bundle_v1": bundle,
            "projects": projects,
            "legacy_confirmed": legacy,
            "confirmation_questions": questions,
            "capabilities": {
                "search": True,
                "owner_action": True,
                "local_handoff": True,
                "product_feedback": True,
            },
            "raw_access": False,
        }
    except ContractError as exc:
        return {
            "contract_version": CONTEXT_SNAPSHOT_VERSION,
            "status": "rejected",
            "observed_at": observed_at,
            "last_successful_read_at": observed_at,
            "actions_enabled": False,
            "counts": None,
            "context_bundle_v1": None,
            "projects": None,
            "legacy_confirmed": None,
            "confirmation_questions": None,
            "capabilities": None,
            "raw_access": False,
        }
    except sqlite3.OperationalError as exc:
        reason = "database_busy" if "lock" in str(exc).lower() or "busy" in str(exc).lower() else "database_unavailable"
        return _phase2_unknown(CONTEXT_SNAPSHOT_VERSION, reason, observed_at)
    except sqlite3.Error:
        return _phase2_unknown(CONTEXT_SNAPSHOT_VERSION, "database_unavailable", observed_at)


def list_memory_candidates(
    conn: sqlite3.Connection,
    raw_payload: bytes | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_at = _iso(_aware_utc(now))
    try:
        with _context_authorization_lock(conn, exclusive=False):
            return _list_memory_candidates_locked(
                conn,
                raw_payload,
                now=now,
            )
    except sqlite3.OperationalError as exc:
        reason = (
            "database_busy"
            if "lock" in str(exc).lower() or "busy" in str(exc).lower()
            else "database_unavailable"
        )
        return _phase2_unknown(
            MEMORY_CANDIDATE_LIST_VERSION,
            reason,
            observed_at,
        )
    except sqlite3.Error:
        return _phase2_unknown(
            MEMORY_CANDIDATE_LIST_VERSION,
            "database_unavailable",
            observed_at,
        )


def _list_memory_candidates_locked(
    conn: sqlite3.Connection,
    raw_payload: bytes | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    from .user_decisions import safe_decision_text

    current = _aware_utc(now)
    observed_at = _iso(current)
    try:
        payload = _phase2_read_payload(raw_payload, _MEMORY_CANDIDATE_LIST_FIELDS)
        project_ref = str(payload.get("project_ref") or "")
        if project_ref:
            _require_active_label(conn, project_ref, "project")
        limit = _phase2_limit(payload.get("limit"), default=100, maximum=200)
        if not context_console_enabled(conn):
            return {
                "contract_version": MEMORY_CANDIDATE_LIST_VERSION,
                "status": "disabled",
                "observed_at": observed_at,
                "last_successful_read_at": observed_at,
                "candidates": None,
                "actions": None,
                "raw_access": False,
            }
        params: list[Any] = []
        where = ""
        if project_ref:
            where = "WHERE m.project_ref=?"
            params.append(project_ref)
        params.append(limit)
        rows = conn.execute(
            "SELECT m.* FROM context_memory_candidates m "
            f"{where} ORDER BY datetime(m.created_at) DESC,m.memory_candidate_ref LIMIT ?",
            tuple(params),
        ).fetchall()
        candidates: list[dict[str, Any]] = []
        for candidate in rows:
            public_context: dict[str, Any] | None = None
            reason_code = ""
            stored_revocation_reason = str(
                candidate["revocation_reason_code"] or ""
            ).casefold()
            stored_expiry = _parse_contract_time(str(candidate["expires_at"]))
            if (
                stored_expiry <= current
                or "expired" in stored_revocation_reason
                or "retention_expired" in stored_revocation_reason
            ):
                lifecycle_state = "expired"
                reason_code = (
                    str(candidate["revocation_reason_code"] or "")
                    or "memory_candidate_expired"
                )
            elif (
                "subject" in stored_revocation_reason
                or "wrong_subject" in stored_revocation_reason
            ):
                lifecycle_state = "wrong_subject_quarantined"
                reason_code = (
                    str(candidate["revocation_reason_code"] or "")
                    or "memory_candidate_subject_invalid"
                )
            else:
                try:
                    verified = _validate_memory_candidate_eligible(
                        conn,
                        candidate,
                        current,
                    )
                    public_context = _public_verified_context(conn, verified)
                    lifecycle_state = "pending_review"
                except ContractError as exc:
                    reason_code = exc.reason_code
                    if "expired" in reason_code:
                        lifecycle_state = "expired"
                    elif "subject" in reason_code:
                        lifecycle_state = "wrong_subject_quarantined"
                    else:
                        lifecycle_state = "withdrawn"
            if public_context is not None:
                project = dict(public_context["project"])
                subject = dict(public_context["subject"])
                safe_summary = safe_decision_text(
                    " · ".join(
                        filter(
                            None,
                            (
                                str(project.get("label") or ""),
                                str(public_context["object"].get("label") or ""),
                                str(public_context.get("state") or ""),
                            ),
                        )
                    ),
                    max_len=220,
                )
                context_ref: str | None = str(public_context["context_ref"])
                public_candidate_ref = _derived_ref(
                    "memory",
                    f"public|{context_ref}|{subject.get('ref') or ''}",
                )
                expires_at = str(candidate["expires_at"])
                revocation_state = "active"
            else:
                project = _public_label(
                    conn,
                    str(candidate["project_ref"] or ""),
                    "project",
                )
                subject = None
                safe_summary = {
                    "expired": "该记忆候选已过期",
                    "wrong_subject_quarantined": "该记忆候选已隔离，主体不符合要求",
                    "withdrawn": "该记忆候选已撤回",
                }[lifecycle_state]
                context_ref = _derived_ref(
                    "context",
                    f"retired-memory|{candidate['memory_candidate_ref']}",
                )
                public_candidate_ref = _derived_ref(
                    "memory",
                    f"retired|{candidate['memory_candidate_ref']}",
                )
                expires_at = str(candidate["expires_at"] or "")
                revocation_state = "expired" if lifecycle_state == "expired" else "revoked"
            candidates.append(
                {
                    "memory_candidate_ref": public_candidate_ref,
                    "context_ref": context_ref,
                    "safe_summary": safe_summary,
                    "project": project,
                    "subject": subject,
                    "state": lifecycle_state,
                    "expires_at": expires_at,
                    "revocation_state": revocation_state,
                    "direct_write": False,
                    "writer_enabled": False,
                    "memory_write_actions": False,
                    "raw_access": False,
                }
            )
        return {
            "contract_version": MEMORY_CANDIDATE_LIST_VERSION,
            "status": "available",
            "observed_at": observed_at,
            "last_successful_read_at": observed_at,
            "candidates": candidates,
            "actions": None,
            "raw_access": False,
        }
    except ContractError as exc:
        return {
            "contract_version": MEMORY_CANDIDATE_LIST_VERSION,
            "status": "rejected",
            "observed_at": observed_at,
            "last_successful_read_at": observed_at,
            "candidates": None,
            "actions": None,
            "raw_access": False,
        }
    except sqlite3.OperationalError as exc:
        reason = "database_busy" if "lock" in str(exc).lower() or "busy" in str(exc).lower() else "database_unavailable"
        return _phase2_unknown(MEMORY_CANDIDATE_LIST_VERSION, reason, observed_at)
    except sqlite3.Error:
        return _phase2_unknown(MEMORY_CANDIDATE_LIST_VERSION, "database_unavailable", observed_at)


def _delivery_failure_category(reason_code: str) -> str:
    if reason_code in {"provider_unavailable"}:
        return "provider_unavailable"
    if reason_code in {"delivery_timeout"}:
        return "timeout"
    if reason_code in {"consumer_rejected", "provider_rejected", "authentication_failed"}:
        return "rejected"
    if reason_code:
        return "unknown"
    return ""


def delivery_status(
    conn: sqlite3.Connection,
    raw_payload: bytes | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_at = _iso(_aware_utc(now))
    try:
        with _context_authorization_lock(conn, exclusive=False):
            return _delivery_status_locked(
                conn,
                raw_payload,
                now=now,
            )
    except sqlite3.OperationalError as exc:
        reason = (
            "database_busy"
            if "lock" in str(exc).lower() or "busy" in str(exc).lower()
            else "database_unavailable"
        )
        return _phase2_unknown(DELIVERY_STATUS_VERSION, reason, observed_at)
    except sqlite3.Error:
        return _phase2_unknown(
            DELIVERY_STATUS_VERSION,
            "database_unavailable",
            observed_at,
        )


def _delivery_status_locked(
    conn: sqlite3.Connection,
    raw_payload: bytes | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    from .activity_confirmations import list_activity_confirmation_cards

    current = _aware_utc(now)
    observed_at = _iso(current)
    try:
        payload = _phase2_read_payload(raw_payload, _DELIVERY_STATUS_FIELDS)
        project_ref = str(payload.get("project_ref") or "")
        target_ref = str(payload.get("target_ref") or "")
        if project_ref:
            _require_active_label(conn, project_ref, "project")
        if target_ref:
            _require_active_label(conn, target_ref, "target")
        limit = _phase2_limit(payload.get("limit"), default=50, maximum=100)
        start_at, end_at = _phase2_time_range(
            conn,
            start_at=payload.get("start_at"),
            end_at=payload.get("end_at"),
            now=current,
        )
        if not context_console_enabled(conn):
            return {
                "contract_version": DELIVERY_STATUS_VERSION,
                "status": "disabled",
                "observed_at": observed_at,
                "last_successful_read_at": observed_at,
                "daily_report": None,
                "deliveries": None,
                "raw_access": False,
            }
        where = [
            "datetime(d.prepared_at)>=datetime(?)",
            "datetime(d.prepared_at)<datetime(?)",
        ]
        params: list[Any] = [start_at, end_at]
        if project_ref:
            where.append("d.project_ref=?")
            params.append(project_ref)
        if target_ref:
            where.append("d.target_ref=?")
            params.append(target_ref)
        where.append(
            "NOT EXISTS (SELECT 1 FROM context_deliveries newer "
            "WHERE newer.bundle_ref=d.bundle_ref AND newer.target_ref=d.target_ref "
            "AND newer.attempt_no>d.attempt_no)"
        )
        params.append(limit)
        rows = conn.execute(
            "SELECT d.*,m.status AS manifest_status,m.restriction_reason_code "
            "FROM context_deliveries d "
            "JOIN context_bundle_manifests m ON m.bundle_ref=d.bundle_ref "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY datetime(d.prepared_at) DESC,d.delivery_ref LIMIT ?",
            tuple(params),
        ).fetchall()
        deliveries: list[dict[str, Any]] = []
        for row in rows:
            withdrawn = bool(
                str(row["manifest_status"] or "") != "active"
                or conn.execute(
                    "SELECT 1 FROM context_withdrawal_receipts WHERE delivery_ref=? LIMIT 1",
                    (str(row["delivery_ref"]),),
                ).fetchone()
            )
            deliveries.append(
                {
                    "delivery_ref": str(row["delivery_ref"]),
                    "bundle_ref": str(row["bundle_ref"]),
                    "attempt_no": int(row["attempt_no"]),
                    "project": _public_label(conn, str(row["project_ref"]), "project"),
                    "target": _public_label(conn, str(row["target_ref"]), "target"),
                    "state": str(row["state"]),
                    "prepared_at": str(row["prepared_at"] or "") or None,
                    "consumed_at": str(row["consumed_at"] or "") or None,
                    "delivered_at": str(row["delivered_at"] or "") or None,
                    "failed_at": str(row["failed_at"] or "") or None,
                    "failure_category": _delivery_failure_category(
                        str(row["failure_reason_code"] or "")
                    ),
                    "withdrawal_state": "restricted" if withdrawn else "active",
                    "raw_access": False,
                }
            )
        confirmation_cards = list_activity_confirmation_cards(
            conn,
            range_name="today",
            start=start_at,
            end=end_at,
            limit=1,
            scan_limit=PHASE2_CONFIRMATION_SCAN_LIMIT,
        )
        pending = int(
            confirmation_cards.get("pending_confirmation_count") or 0
        )
        # A truncated legacy candidate window must never make an unfinished
        # report look ready.  One is a conservative public lower bound; the
        # exact legacy diagnostic count remains available through its existing
        # non-Phase2 contract.
        if confirmation_cards.get("candidate_scan_truncated"):
            pending = max(1, pending)
        latest = next(
            (
                delivery
                for delivery in deliveries
                if delivery["withdrawal_state"] == "active"
            ),
            None,
        )
        needs_input = pending > 0 or latest is None
        current_state = (
            "needs_input"
            if needs_input
            else str(latest["state"])
        )
        daily_report = {
            "needs_input": needs_input,
            "pending_confirmation_count": pending,
            "current_state": current_state,
            "prepared_at": latest.get("prepared_at") if latest else None,
            "consumed_at": latest.get("consumed_at") if latest else None,
            "delivered_at": latest.get("delivered_at") if latest else None,
            "failed_at": latest.get("failed_at") if latest else None,
        }
        return {
            "contract_version": DELIVERY_STATUS_VERSION,
            "status": "available",
            "observed_at": observed_at,
            "last_successful_read_at": observed_at,
            "daily_report": daily_report,
            "deliveries": deliveries,
            "raw_access": False,
        }
    except ContractError as exc:
        return {
            "contract_version": DELIVERY_STATUS_VERSION,
            "status": "rejected",
            "observed_at": observed_at,
            "last_successful_read_at": observed_at,
            "daily_report": None,
            "deliveries": None,
            "raw_access": False,
        }
    except sqlite3.OperationalError as exc:
        reason = "database_busy" if "lock" in str(exc).lower() or "busy" in str(exc).lower() else "database_unavailable"
        return _phase2_unknown(DELIVERY_STATUS_VERSION, reason, observed_at)
    except sqlite3.Error:
        return _phase2_unknown(DELIVERY_STATUS_VERSION, "database_unavailable", observed_at)


def build_local_handoff(
    conn: sqlite3.Connection,
    raw_payload: bytes | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    # Keep injected time deterministic, but production time is a trust input
    # and must be refreshed after each authorization wait. There are two leases
    # here: one to build the projection and one to persist it after a possible
    # exclusive revoke/delete has run between those phases.
    injected_current = _aware_utc(now) if now is not None else None
    current = injected_current if injected_current is not None else _aware_utc(None)
    observed_at = _iso(current)
    try:
        payload = _phase2_read_payload(raw_payload, _LOCAL_HANDOFF_FIELDS)
        project_ref = str(payload.get("project_ref") or "")
        if not project_ref:
            raise ContractError("project_ref_required")
        query = _validate_search_query(payload.get("query") or "")
        limit = _phase2_limit(payload.get("limit"), default=100, maximum=200)
        with _context_authorization_lock(conn, exclusive=False):
            current = injected_current if injected_current is not None else _aware_utc(None)
            observed_at = _iso(current)
            if not context_console_enabled(conn):
                return {
                    "contract_version": LOCAL_HANDOFF_VERSION,
                    "status": "disabled",
                    "observed_at": observed_at,
                    "last_successful_read_at": observed_at,
                    "handoff_ref": None,
                    "bundle_hash": None,
                    "project": None,
                    "verified_contexts": None,
                    "unresolved_questions": None,
                    "expires_at": None,
                    "revocation_state": None,
                    "raw_access": False,
                }
            _require_active_label(conn, project_ref, "project")
            start_at, end_at = _phase2_time_range(
                conn,
                start_at=payload.get("start_at"),
                end_at=payload.get("end_at"),
                query=query,
                now=current,
            )
            bundle, private_members = _materialize_context_bundle_locked(
                conn,
                project_ref=project_ref,
                start_at=start_at,
                end_at=end_at,
                query=query,
                include_clues=False,
                limit=limit,
                now=current,
            )
            if bundle.get("status") != "available":
                raise ContractError(str(bundle.get("reason_code") or "bundle_unavailable"))
            if not private_members:
                raise ContractError("verified_context_required")
        bundle_hash = str(bundle["bundle_hash"])
        handoff_ref = _derived_ref(
            "bundle",
            f"local_handoff|{project_ref}|{bundle_hash}",
        )
        frozen_payload = dict(bundle["frozen_payload"])
        stable_projection_json = json.dumps(
            frozen_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with _context_authorization_lock(conn, exclusive=False):
            conn.execute("BEGIN IMMEDIATE")
            # Production time is sampled only after SQLite grants the writer
            # lock. The lock wait itself can cross a source expiry or local-day
            # boundary. Injected replay time intentionally remains fixed.
            current = injected_current if injected_current is not None else _aware_utc(None)
            observed_at = _iso(current)
            created_at = observed_at
            if not context_console_enabled(conn):
                raise ContractError("feature_flag_off")
            _require_active_label(conn, project_ref, "project")
            # The read projection may have been built immediately before an
            # exclusive delete/revoke lease. Revalidate every private member
            # under a fresh shared lease before persisting the real handoff.
            for private_ref, _ in private_members:
                graph = _load_verified_graph(conn, private_ref)
                if not graph:
                    raise ContractError("local_handoff_source_revoked")
                try:
                    _validate_verified_graph(conn, graph, current)
                except ContractError as exc:
                    raise ContractError("local_handoff_source_revoked") from exc
            # Questions are not part of the trusted frozen projection, but they
            # are still user-visible. Recompute them at the same fresh trust
            # timestamp so an item expiring during either wait is not returned.
            questions = _confirmation_questions(
                conn,
                start_at=start_at,
                end_at=end_at,
                project_ref=project_ref,
                now=current,
            )
            existing = conn.execute(
                "SELECT * FROM context_bundle_manifests WHERE bundle_ref=?",
                (handoff_ref,),
            ).fetchone()
            if existing:
                if str(existing["bundle_hash"] or "") != bundle_hash:
                    raise ContractError("local_handoff_conflict")
                try:
                    _validate_bundle_manifest_live(conn, existing, current)
                except ContractError as exc:
                    raise ContractError("local_handoff_revoked") from exc
            else:
                conn.execute(
                    "INSERT INTO context_bundle_manifests(bundle_ref,bundle_hash,project_ref,start_at,end_at,"
                    "query_fingerprint,target_ref,item_count,stable_projection_json,frozen_payload_json,status,created_at) "
                    "VALUES(?,?,?,?,?,?, '',?,?,?,'active',?)",
                    (
                        handoff_ref,
                        bundle_hash,
                        project_ref,
                        start_at,
                        end_at,
                        str(bundle["filters"].get("query_fingerprint") or ""),
                        len(private_members),
                        stable_projection_json,
                        stable_projection_json,
                        created_at,
                    ),
                )
                for private_ref, public_item in private_members:
                    conn.execute(
                        "INSERT INTO context_bundle_members(bundle_ref,member_kind,member_ref,member_hash,status) "
                        "VALUES(?,'verified_context',?,?,'active')",
                        (
                            handoff_ref,
                            private_ref,
                            _public_hash(public_item),
                        ),
                    )
            conn.commit()
        verified = sorted(
            list(bundle.get("verified_contexts") or []),
            key=lambda item: (
                str(item.get("occurred_at") or ""),
                str(item.get("context_ref") or ""),
            ),
            reverse=True,
        )
        expires_at = min(
            (str(item.get("expires_at") or "") for item in verified if item.get("expires_at")),
            default=None,
        )
        return {
            "contract_version": LOCAL_HANDOFF_VERSION,
            "status": "available",
            "observed_at": observed_at,
            "last_successful_read_at": observed_at,
            "handoff_ref": handoff_ref,
            "bundle_hash": bundle_hash,
            "project": _public_label(conn, project_ref, "project"),
            "verified_contexts": verified,
            "unresolved_questions": [
                str(question.get("question") or "")
                for question in questions
                if str(question.get("question") or "")
            ],
            "expires_at": expires_at,
            "revocation_state": "active",
            "raw_access": False,
        }
    except ContractError as exc:
        if conn.in_transaction:
            conn.rollback()
        return {
            "contract_version": LOCAL_HANDOFF_VERSION,
            "status": "disabled" if exc.reason_code == "feature_flag_off" else "rejected",
            "observed_at": observed_at,
            "last_successful_read_at": observed_at,
            "handoff_ref": None,
            "bundle_hash": None,
            "project": None,
            "verified_contexts": None,
            "unresolved_questions": None,
            "expires_at": None,
            "revocation_state": None,
            "raw_access": False,
        }
    except sqlite3.OperationalError as exc:
        if conn.in_transaction:
            conn.rollback()
        reason = "database_busy" if "lock" in str(exc).lower() or "busy" in str(exc).lower() else "database_unavailable"
        return _phase2_unknown(LOCAL_HANDOFF_VERSION, reason, observed_at)
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _phase2_unknown(LOCAL_HANDOFF_VERSION, "database_unavailable", observed_at)
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def _owner_action_result(
    *,
    status: str,
    reason_code: str = "",
    action_ref: str | None = None,
    confirmation_ref: str | None = None,
    confirmation_revision: str | None = None,
    context_ref: str | None = None,
    verification_state: str | None = None,
) -> dict[str, Any]:
    return {
        "contract_version": OWNER_CONTEXT_ACTION_VERSION,
        "status": status,
        "action_ref": action_ref,
        "confirmation_ref": confirmation_ref,
        "confirmation_revision": confirmation_revision,
        "context_ref": context_ref,
        "verification_state": verification_state,
        "raw_access": False,
        "memory_write_actions": False,
        "writer_enabled": False,
        "direct_write": False,
    }


def _resolve_public_confirmation(
    conn: sqlite3.Connection,
    *,
    confirmation_ref: str,
    current: datetime,
) -> dict[str, Any] | None:
    from .activity_confirmations import list_activity_confirmation_cards

    # Resolve today and yesterday independently. Combining both days can
    # re-cluster a card at midnight and change the revision that Swift was
    # shown. Each retained day is bounded to the same 50-card public surface,
    # so the last visible inbox item remains actionable without an unbounded
    # private-table scan.
    today_start, today_end = _phase2_time_range(conn, now=current)
    today_start_time = _parse_contract_time(today_start)
    today_end_time = _parse_contract_time(today_end)
    retained_ranges = (
        ("today", today_start, today_end),
        (
            "yesterday",
            _iso(today_start_time - timedelta(days=1)),
            _iso(today_end_time - timedelta(days=1)),
        ),
    )
    for range_name, start_at, end_at in retained_ranges:
        cards = list_activity_confirmation_cards(
            conn,
            range_name=range_name,
            start=start_at,
            end=end_at,
            limit=50,
            scan_limit=PHASE2_CONFIRMATION_SCAN_LIMIT,
        )
        for card in list(cards.get("cards") or []):
            if str(card.get("section") or "") != "pending_confirmation":
                continue
            revision = str(card.get("member_revision") or "")
            public_ref = _derived_ref(
                "confirmation",
                f"{card.get('group_key') or ''}|{revision}",
            )
            if hmac.compare_digest(public_ref, confirmation_ref):
                return dict(card)
    return None


def _ensure_owner_action_principal_locked(
    conn: sqlite3.Connection,
    *,
    project_ref: str,
    current: datetime,
) -> Principal:
    principal_id = _derived_ref("principal", f"owner-context-action|{project_ref}")
    key_id = _derived_ref("key", f"owner-context-action|{project_ref}|v1")
    fingerprint = _hash_text(f"local-owner-action-v1|{project_ref}")
    existing = conn.execute(
        "SELECT principal_kind,project_ref,target_ref,target_scope,key_fingerprint,status "
        "FROM context_principals WHERE principal_id=? AND key_id=?",
        (principal_id, key_id),
    ).fetchone()
    if existing:
        exact = (
            str(existing["status"] or "") == "active"
            and str(existing["principal_kind"] or "") == "user_confirmation"
            and str(existing["project_ref"] or "") == project_ref
            and not str(existing["target_ref"] or "")
            and not str(existing["target_scope"] or "")
            and hmac.compare_digest(
                str(existing["key_fingerprint"] or ""),
                fingerprint,
            )
        )
        if not exact:
            raise ContractError("owner_action_principal_conflict")
    else:
        conn.execute(
            "INSERT INTO context_principals(principal_id,key_id,principal_kind,project_ref,target_ref,target_scope,"
            "key_fingerprint,status,created_at,revoked_at) "
            "VALUES(?,?,'user_confirmation',?,'','',?,'active',?,NULL)",
            (
                principal_id,
                key_id,
                project_ref,
                fingerprint,
                _iso(current),
            ),
        )
    return Principal(
        principal_id=principal_id,
        key_id=key_id,
        principal_kind="user_confirmation",
        project_ref=project_ref,
        target_ref="",
        target_scope="",
        key_fingerprint=fingerprint,
    )


def _validate_owner_context_refs(
    conn: sqlite3.Connection,
    payload: Mapping[str, Any],
) -> None:
    refs = {
        field: str(payload.get(field) or "")
        for field in (
            "project_ref",
            "workstream_ref",
            "object_ref",
            "version_ref",
            "subject_ref",
        )
    }
    _validate_registry_refs(conn, refs)
    subject = _require_active_label(conn, refs["subject_ref"], "subject")
    if str(subject["subject_scope"] or "") != "owner":
        raise ContractError("owner_subject_required")
    if payload.get("privacy_class") not in PRIVACY_CLASSES:
        raise ContractError("invalid_privacy_class")
    if payload.get("context_state") not in CONTEXT_STATES:
        raise ContractError("invalid_context_state")


def _materialize_owner_action_context_locked(
    conn: sqlite3.Connection,
    *,
    normalized: Mapping[str, Any],
    confirmation_ref: str,
    occurred_at: str,
    current: datetime,
    action_ref: str,
    action_payload_hash: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    project_ref = str(normalized["project_ref"])
    principal = _ensure_owner_action_principal_locked(
        conn,
        project_ref=project_ref,
        current=current,
    )
    source_ref = _derived_ref(
        "source",
        f"owner-context-action|{confirmation_ref}|{action_ref}",
    )
    materialization = {
        "contract_version": CONTROLLED_EVIDENCE_VERSION,
        "idempotency_key": str(normalized["idempotency_key"]),
        "principal_id": principal.principal_id,
        "key_id": principal.key_id,
        "source_ref": source_ref,
        "occurred_at": _iso(_parse_contract_time(occurred_at)),
        "project_ref": project_ref,
        "workstream_ref": str(normalized["workstream_ref"]),
        "object_ref": str(normalized["object_ref"]),
        "version_ref": str(normalized["version_ref"]),
        "subject_ref": str(normalized["subject_ref"]),
        "privacy_class": str(normalized["privacy_class"]),
        "evidence_kind": "user_confirmation",
        "state": str(normalized["context_state"]),
        "provider_receipt_ref": "",
        "verification_scope": "operational",
        "memory_eligible": False,
        "signature": "hmac-sha256:" + ("0" * 64),
    }
    occurred = _parse_contract_time(str(materialization["occurred_at"]))
    if _parse_contract_time(_verified_expiry(conn, occurred)) <= current:
        raise ContractError("owner_action_source_expired")
    source_event_ref = _canonical_source_event_ref(
        OWNER_CONTEXT_ACTION_VERSION,
        principal.principal_id,
        materialization,
    )
    if conn.execute(
        "SELECT 1 FROM context_source_event_tombstones WHERE source_event_ref=?",
        (source_event_ref,),
    ).fetchone():
        raise ContractError("source_event_tombstoned")
    receipt_ref = _derived_ref(
        "receipt",
        f"{OWNER_CONTEXT_ACTION_VERSION}|{action_ref}|{action_payload_hash}",
    )
    refs = _materialize_controlled_evidence(
        conn,
        materialization,
        action_payload_hash,
        current,
        principal,
        source_event_ref=source_event_ref,
        receipt_ref=receipt_ref,
    )
    semantic_hash = _source_event_semantic_hash(
        OWNER_CONTEXT_ACTION_VERSION,
        materialization,
    )
    _insert_source_event_locked(
        conn,
        contract_type=OWNER_CONTEXT_ACTION_VERSION,
        principal=principal,
        payload=materialization,
        semantic_hash=semantic_hash,
        source_event_ref=source_event_ref,
        observation_ref=str(refs["observation_ref"]),
        current=current,
    )
    _insert_ingest_receipt_locked(
        conn,
        normalized=materialization,
        principal=principal,
        contract_type=OWNER_CONTEXT_ACTION_VERSION,
        payload_hash=action_payload_hash,
        receipt_ref=receipt_ref,
        source_event_ref=source_event_ref,
        observation_ref=str(refs["observation_ref"]),
        result_status=str(refs["result_status"]),
        current=current,
    )
    verified = _load_verified_graph(conn, str(refs["verified_ref"]))
    if not verified:
        raise ContractError("verified_context_required")
    _validate_verified_graph(conn, verified, current)
    return refs, _public_verified_context(conn, verified)


def _ensure_owner_derived_label_locked(
    conn: sqlite3.Connection,
    *,
    kind: str,
    ref: str,
    label: str,
    project_ref: str,
    current: datetime,
    subject_scope: str = "",
    extended_manual_label: bool = False,
) -> None:
    """Insert one deterministic owner-confirmation label without committing.

    This intentionally does not call the public label registration API: that
    API owns its transaction. Confirm-card promotion must keep derived labels,
    the legacy answer, Context graph, and link in one caller-owned transaction.
    """
    if kind not in {"project", "workstream", "object", "version", "subject"}:
        raise ContractError("invalid_label_kind")
    _validate_ref_for_kind(kind, ref)
    clean_label = (
        _validate_manual_note_label(label)
        if extended_manual_label
        else _validate_approved_label(label)
    )
    expected_project = ref if kind == "project" else project_ref
    if kind != "project":
        _validate_opaque_ref("project_ref", expected_project)
        project = _label_row(conn, expected_project, "project")
        if not project:
            raise ContractError("project_unregistered")
    if kind == "subject":
        if subject_scope != "owner":
            raise ContractError("owner_subject_required")
    elif subject_scope:
        raise ContractError("subject_scope_not_allowed")
    existing = conn.execute(
        "SELECT kind,label,project_ref,subject_scope,status "
        "FROM context_label_registry WHERE ref=?",
        (ref,),
    ).fetchone()
    if existing:
        exact = (
            str(existing["kind"] or "") == kind
            and str(existing["label"] or "") == clean_label
            and str(existing["project_ref"] or "") == expected_project
            and str(existing["subject_scope"] or "") == subject_scope
            and str(existing["status"] or "") == "active"
        )
        if not exact:
            raise ContractError("derived_label_conflict")
        return
    conn.execute(
        "INSERT INTO context_label_registry(ref,kind,label,project_ref,subject_scope,status,created_at,revoked_at) "
        "VALUES(?,?,?,?,?,'active',?,NULL)",
        (
            ref,
            kind,
            clean_label,
            expected_project,
            subject_scope,
            _iso(current),
        ),
    )


def _derive_confirmation_context_locked(
    conn: sqlite3.Connection,
    *,
    card: Mapping[str, Any],
    confirmation_ref: str,
    current: datetime,
    register_labels: bool,
) -> dict[str, str]:
    from .user_decisions import safe_decision_text

    group_key = str(card.get("group_key") or "")
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT suggested_workstream,suggested_outcome,user_workstream,user_outcome "
            "FROM activity_confirmation_items WHERE group_key=? "
            "ORDER BY datetime(observed_start),item_key",
            (group_key,),
        ).fetchall()
    ]
    if not rows:
        raise ContractError("confirmation_not_found_or_stale")
    subject = dict(card.get("canonical_subject") or {})
    object_label = safe_decision_text(
        subject.get("object") or subject.get("label") or "",
        max_len=80,
    )
    if not object_label:
        raise ContractError("information_insufficient")
    workstream_label = next(
        (
            safe_decision_text(
                row.get("user_workstream") or row.get("suggested_workstream") or "",
                max_len=80,
            )
            for row in rows
            if safe_decision_text(
                row.get("user_workstream") or row.get("suggested_workstream") or "",
                max_len=80,
            )
        ),
        object_label,
    )
    outcome = next(
        (
            str(row.get("user_outcome") or row.get("suggested_outcome") or "")
            for row in rows
            if str(row.get("user_outcome") or row.get("suggested_outcome") or "")
        ),
        "unknown",
    )
    if outcome not in {
        "unknown",
        "progressing",
        "blocked",
        "completed",
        "preparation_only",
    }:
        outcome = "unknown"
    context_state = {
        "completed": "completed_confirmed",
        "blocked": "blocked",
        "progressing": "in_progress",
        "preparation_only": "intent_observed",
        "unknown": "intent_observed",
    }[outcome]
    project_label = workstream_label
    project_ref = _derived_ref(
        "project",
        f"owner-confirmation|project|{project_label.casefold()}",
    )
    workstream_ref = _derived_ref(
        "workstream",
        f"owner-confirmation|{project_ref}|workstream|{workstream_label.casefold()}",
    )
    object_ref = _derived_ref(
        "object",
        f"owner-confirmation|{project_ref}|object|{object_label.casefold()}",
    )
    version_ref = _derived_ref(
        "version",
        f"owner-confirmation|{confirmation_ref}|{card.get('member_revision') or ''}",
    )
    subject_ref = _derived_ref(
        "subject",
        f"owner-confirmation|{project_ref}|owner",
    )
    if register_labels:
        _ensure_owner_derived_label_locked(
            conn,
            kind="project",
            ref=project_ref,
            label=project_label,
            project_ref=project_ref,
            current=current,
        )
        for kind, ref, label in (
            ("workstream", workstream_ref, workstream_label),
            ("object", object_ref, object_label),
            ("version", version_ref, "用户确认版本"),
        ):
            _ensure_owner_derived_label_locked(
                conn,
                kind=kind,
                ref=ref,
                label=label,
                project_ref=project_ref,
                current=current,
            )
        _ensure_owner_derived_label_locked(
            conn,
            kind="subject",
            ref=subject_ref,
            label="Local Owner",
            project_ref=project_ref,
            current=current,
            subject_scope="owner",
        )
    return {
        "project_ref": project_ref,
        "workstream_ref": workstream_ref,
        "object_ref": object_ref,
        "version_ref": version_ref,
        "subject_ref": subject_ref,
        "privacy_class": "private_local",
        "context_state": context_state,
        "workstream": workstream_label,
        "outcome": outcome,
    }


def _derive_manual_note_context_locked(
    conn: sqlite3.Connection,
    *,
    normalized: Mapping[str, Any],
    current: datetime,
) -> dict[str, str]:
    safe_label = _validate_manual_note_label(normalized.get("safe_label"))
    supplied_project_ref = str(normalized.get("project_ref") or "")
    supplied_project_label = str(normalized.get("project_label") or "")
    if supplied_project_ref:
        project = _require_active_label(conn, supplied_project_ref, "project")
        project_ref = supplied_project_ref
        project_label = str(project["label"] or "")
        if supplied_project_label:
            requested_label = _validate_approved_label(supplied_project_label)
            if requested_label != project_label:
                raise ContractError("manual_project_label_mismatch")
    else:
        project_label = _validate_approved_label(
            supplied_project_label or "未归类"
        )
        project_ref = _derived_ref(
            "project",
            f"owner-manual-note|project|{project_label.casefold()}",
        )
        _ensure_owner_derived_label_locked(
            conn,
            kind="project",
            ref=project_ref,
            label=project_label,
            project_ref=project_ref,
            current=current,
        )
    workstream_label = _validate_approved_label(
        normalized.get("workstream") or project_label
    )
    outcome = str(normalized.get("outcome") or "unknown")
    context_state_by_outcome = {
        "completed": "completed_confirmed",
        "blocked": "blocked",
        "progressing": "in_progress",
        "preparation_only": "in_progress",
        "unknown": "intent_observed",
    }
    if outcome not in context_state_by_outcome:
        raise ContractError("invalid_manual_outcome")
    workstream_ref = _derived_ref(
        "workstream",
        f"owner-manual-note|{project_ref}|workstream|{workstream_label.casefold()}",
    )
    object_ref = _derived_ref(
        "object",
        f"owner-manual-note|{project_ref}|object|{safe_label.casefold()}",
    )
    version_ref = _derived_ref(
        "version",
        f"owner-manual-note|{normalized['idempotency_key']}|version",
    )
    subject_ref = _derived_ref(
        "subject",
        f"owner-manual-note|{project_ref}|owner",
    )
    for kind, ref, label in (
        ("workstream", workstream_ref, workstream_label),
        ("object", object_ref, safe_label),
        ("version", version_ref, "本人补记版本"),
    ):
        _ensure_owner_derived_label_locked(
            conn,
            kind=kind,
            ref=ref,
            label=label,
            project_ref=project_ref,
            current=current,
            extended_manual_label=(kind == "object"),
        )
    _ensure_owner_derived_label_locked(
        conn,
        kind="subject",
        ref=subject_ref,
        label="Local Owner",
        project_ref=project_ref,
        current=current,
        subject_scope="owner",
    )
    return {
        "project_ref": project_ref,
        "project_label": project_label,
        "workstream_ref": workstream_ref,
        "object_ref": object_ref,
        "version_ref": version_ref,
        "subject_ref": subject_ref,
        "privacy_class": "private_local",
        "context_state": context_state_by_outcome[outcome],
        "workstream": workstream_label,
        "outcome": outcome,
        "safe_label": safe_label,
    }


def _record_owner_context_action_locked(
    conn: sqlite3.Connection,
    *,
    action: str,
    idempotency_key: str,
    confirmation_ref: str,
    answer: str,
    verifying: bool,
    normalized: Mapping[str, Any],
    action_payload_hash: str,
    current: datetime,
) -> dict[str, Any]:
    from .activity_confirmations import (
        _answer_activity_confirmation_group_locked,
        _live_confirmation_rows,
        _member_revision,
        _purge_evidence_after_commit,
        _public_confirmation_card_v5,
        manual_add_confirmed_activity,
    )

    conn.execute("BEGIN IMMEDIATE")
    existing = conn.execute(
        "SELECT * FROM context_activity_confirmation_links WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing:
        if not hmac.compare_digest(
            str(existing["payload_hash"] or ""),
            action_payload_hash,
        ):
            raise ContractError("idempotency_conflict")
        stored_status = str(existing["status"] or "")
        stored_verified_ref = str(existing["verified_ref"] or "")
        if stored_verified_ref:
            graph = _load_verified_graph(conn, stored_verified_ref)
            try:
                if stored_status != "verified" or not graph:
                    raise ContractError("owner_action_replay_stale")
                _validate_verified_graph(conn, graph, current)
            except ContractError as exc:
                raise ContractError("owner_action_replay_stale") from exc
        elif stored_status != "non_verifying":
            raise ContractError("owner_action_replay_stale")
        conn.rollback()
        return _owner_action_result(
            status="replayed",
            reason_code="idempotent_replay",
            action_ref=str(existing["link_ref"]),
            confirmation_ref=str(existing["confirmation_ref"]),
            confirmation_revision=str(
                existing["confirmation_revision"] or ""
            ) or None,
            context_ref=str(existing["public_context_ref"] or "") or None,
            verification_state=(
                "verified"
                if str(existing["public_context_ref"] or "")
                else "non_verifying"
            ),
        )

    effective_normalized = dict(normalized)
    private_confirmation_key = ""
    confirmation_revision = ""
    occurred_at = ""
    superseded_link: sqlite3.Row | None = None
    if action == "confirm_card":
        superseded_link = conn.execute(
            "SELECT * FROM context_activity_confirmation_links "
            "WHERE confirmation_ref=? AND status IN ('verified','non_verifying') "
            "ORDER BY datetime(created_at) DESC,link_ref DESC LIMIT 1",
            (confirmation_ref,),
        ).fetchone()
        member_revision_for_write = ""
        if superseded_link:
            confirmation_revision = str(
                superseded_link["confirmation_revision"] or ""
            )
            private_confirmation_key = str(
                superseded_link["confirmation_key"] or ""
            )
            linked_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM activity_confirmation_items "
                    "WHERE group_key=? ORDER BY datetime(observed_start),item_key",
                    (private_confirmation_key,),
                ).fetchall()
            ]
            linked_answer = str(superseded_link["answer"] or "")
            if (
                not linked_rows
                or any(
                    str(row.get("answer") or "") != linked_answer
                    for row in linked_rows
                )
            ):
                raise ContractError("confirmation_not_found_or_stale")
            live_rows = _live_confirmation_rows(conn, linked_rows)
            if len(live_rows) != len(linked_rows):
                raise ContractError("confirmation_not_found_or_stale")
            if str(superseded_link["verified_ref"] or ""):
                graph = _load_verified_graph(
                    conn,
                    str(superseded_link["verified_ref"]),
                )
                if not graph:
                    raise ContractError("confirmation_not_found_or_stale")
                _validate_verified_graph(conn, graph, current)
            card = _public_confirmation_card_v5(
                private_confirmation_key,
                live_rows,
            )
            member_revision_for_write = _member_revision(live_rows)
        else:
            historical_link = conn.execute(
                "SELECT 1 FROM context_activity_confirmation_links "
                "WHERE confirmation_ref=? LIMIT 1",
                (confirmation_ref,),
            ).fetchone()
            if historical_link:
                # A deleted, expired, or already superseded source cannot be
                # revived through a fresh idempotency key.
                raise ContractError("confirmation_not_found_or_stale")
            card = _resolve_public_confirmation(
                conn,
                confirmation_ref=confirmation_ref,
                current=current,
            )
            if not card:
                raise ContractError("confirmation_not_found_or_stale")
            confirmation_revision = str(card.get("member_revision") or "")
            member_revision_for_write = confirmation_revision
            private_confirmation_key = str(card.get("group_key") or "")
        if not hmac.compare_digest(
            confirmation_revision,
            str(normalized["expected_revision"]),
        ):
            raise ContractError("confirmation_revision_stale")
        original_member_keys = {
            str(row["item_key"])
            for row in conn.execute(
                "SELECT item_key FROM activity_confirmation_items "
                "WHERE group_key=? ORDER BY item_key",
                (private_confirmation_key,),
            ).fetchall()
        }
        derived = _derive_confirmation_context_locked(
            conn,
            card=card,
            confirmation_ref=confirmation_ref,
            current=current,
            register_labels=verifying,
        )
        effective_normalized.update(derived)
        if verifying:
            _validate_owner_context_refs(conn, effective_normalized)
        result = _answer_activity_confirmation_group_locked(
            conn,
            group_key=private_confirmation_key,
            member_revision=member_revision_for_write,
            answer=answer,
            reason=str(normalized.get("reason") or "other"),
            workstream=str(derived["workstream"]),
            outcome=(
                str(derived["outcome"])
                if answer == "occurred"
                else "unknown"
            ),
            generalize_scope="exact",
            allow_context_reanswer=bool(superseded_link),
        )
        if (
            str(result.get("status") or "") == "stale"
            and str(result.get("error") or "")
            in {"group_members_changed", "group_regrouped"}
        ):
            refreshed_group_key = str(result.get("group_key") or "")
            refreshed_revision = str(result.get("member_revision") or "")
            refreshed_member_keys = {
                str(row["item_key"])
                for row in conn.execute(
                    "SELECT item_key FROM activity_confirmation_items "
                    "WHERE group_key=? ORDER BY item_key",
                    (refreshed_group_key,),
                ).fetchall()
            }
            if (
                original_member_keys
                and original_member_keys == refreshed_member_keys
                and refreshed_revision
            ):
                private_confirmation_key = refreshed_group_key
                result = _answer_activity_confirmation_group_locked(
                    conn,
                    group_key=refreshed_group_key,
                    member_revision=refreshed_revision,
                    answer=answer,
                    reason=str(normalized.get("reason") or "other"),
                    workstream=str(derived["workstream"]),
                    outcome=(
                        str(derived["outcome"])
                        if answer == "occurred"
                        else "unknown"
                    ),
                    generalize_scope="exact",
                    allow_context_reanswer=bool(superseded_link),
                )
        if str(result.get("status") or "") not in {"recorded"}:
            raise ContractError(str(result.get("error") or "confirmation_write_failed"))
        confirmation_revision = _derived_ref(
            "revision",
            "owner-confirmation-generation|"
            f"{confirmation_ref}|{idempotency_key}|{action_payload_hash}",
        )
        if superseded_link:
            old_observation_ref = str(
                superseded_link["observation_ref"] or ""
            )
            if old_observation_ref:
                _cascade_observation_locked(
                    conn,
                    old_observation_ref,
                    terminal_status="revoked",
                    reason_code="owner_reanswered",
                    now=current,
                    tombstone_source=False,
                )
            else:
                stamp = _iso(current)
                conn.execute(
                    "UPDATE context_activity_confirmation_links "
                    "SET status='revoked',revoked_at=?,"
                    "revocation_reason_code='owner_reanswered',updated_at=? "
                    "WHERE link_ref=? AND status='non_verifying'",
                    (stamp, stamp, str(superseded_link["link_ref"])),
                )
        occurred_at = str(card.get("observed_end") or card.get("observed_start") or "")
    else:
        derived = _derive_manual_note_context_locked(
            conn,
            normalized=effective_normalized,
            current=current,
        )
        effective_normalized.update(derived)
        _validate_owner_context_refs(conn, effective_normalized)
        result = manual_add_confirmed_activity(
            conn,
            confirmation_action="MANUAL_CONFIRMED_ACTIVITY",
            safe_label=str(effective_normalized.get("safe_label") or ""),
            workstream=str(effective_normalized.get("workstream") or ""),
            outcome=str(effective_normalized.get("outcome") or "unknown"),
            started_at=str(effective_normalized.get("started_at") or ""),
            ended_at=str(effective_normalized.get("ended_at") or "") or None,
            _authorization_lock_held=True,
        )
        if str(result.get("status") or "") != "recorded":
            raise ContractError(str(result.get("error") or "manual_confirmation_write_failed"))
        private_confirmation_key = str(
            (result.get("item") or {}).get("item_key") or ""
        )
        if not private_confirmation_key:
            raise ContractError("manual_confirmation_key_missing")
        confirmation_revision = _derived_ref(
            "revision",
            f"manual|{private_confirmation_key}",
        )
        confirmation_ref = _derived_ref(
            "confirmation",
            f"{private_confirmation_key}|{confirmation_revision}",
        )
        occurred_at = str(normalized.get("started_at") or "")

    conflict = conn.execute(
        "SELECT payload_hash FROM context_activity_confirmation_links "
        "WHERE confirmation_ref=? AND status IN ('verified','non_verifying')",
        (confirmation_ref,),
    ).fetchone()
    if conflict:
        if hmac.compare_digest(
            str(conflict["payload_hash"] or ""),
            action_payload_hash,
        ):
            raise ContractError("confirmation_already_linked")
        raise ContractError("confirmation_action_conflict")

    action_ref = _derived_ref(
        "action",
        f"{confirmation_ref}|{idempotency_key}|{action_payload_hash}",
    )
    refs: dict[str, Any] = {
        "observation_ref": "",
        "evidence_ref": "",
        "candidate_ref": "",
        "verified_ref": "",
    }
    public_context: dict[str, Any] | None = None
    link_status = "non_verifying"
    if verifying:
        refs, public_context = _materialize_owner_action_context_locked(
            conn,
            normalized=effective_normalized,
            confirmation_ref=confirmation_ref,
            occurred_at=occurred_at,
            current=current,
            action_ref=action_ref,
            action_payload_hash=action_payload_hash,
        )
        link_status = "verified"
    conn.execute(
        "INSERT INTO context_activity_confirmation_links(link_ref,confirmation_ref,confirmation_kind,"
        "confirmation_key,confirmation_revision,idempotency_key,payload_hash,answer,observation_ref,evidence_ref,"
        "candidate_ref,verified_ref,public_context_ref,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            action_ref,
            confirmation_ref,
            action,
            private_confirmation_key,
            confirmation_revision,
            idempotency_key,
            action_payload_hash,
            answer,
            str(refs.get("observation_ref") or ""),
            str(refs.get("evidence_ref") or ""),
            str(refs.get("candidate_ref") or ""),
            str(refs.get("verified_ref") or ""),
            str((public_context or {}).get("context_ref") or ""),
            link_status,
            _iso(current),
            _iso(current),
        ),
    )
    conn.commit()
    if action == "confirm_card" and answer != "defer":
        _purge_evidence_after_commit(conn)
    return _owner_action_result(
        status="recorded",
        action_ref=action_ref,
        confirmation_ref=confirmation_ref,
        confirmation_revision=confirmation_revision,
        context_ref=str((public_context or {}).get("context_ref") or "") or None,
        verification_state="verified" if verifying else "non_verifying",
    )


def _rollback_owner_action_or_discard_connection(
    conn: sqlite3.Connection,
) -> None:
    """End the owned transaction or make its connection unusable.

    Cleanup must never replace the process-control exception handled by the
    public owner-action boundary.  A failed rollback (or a transaction that
    remains active after rollback) makes the caller-supplied connection unsafe
    to reuse, so discard it instead of allowing a later commit to persist the
    partial legacy/context dual write.
    """

    discard_connection = False
    try:
        transaction_active = bool(conn.in_transaction)
    except BaseException:
        transaction_active = True
        discard_connection = True
    if transaction_active and not discard_connection:
        try:
            conn.rollback()
        except BaseException:
            discard_connection = True
        else:
            try:
                discard_connection = bool(conn.in_transaction)
            except BaseException:
                discard_connection = True
    if discard_connection:
        try:
            conn.close()
        except BaseException:
            # Preserve the original process-control exception even if the
            # best-effort connection discard reports its own cleanup failure.
            pass


def owner_context_action(
    conn: sqlite3.Connection,
    raw_payload: bytes | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _aware_utc(now)
    try:
        payload = _parse_closed_json(
            raw_payload,
            _OWNER_CONTEXT_ACTION_FIELDS,
            required_fields={
                "contract_version",
                "action",
                "idempotency_key",
                "confirmation_action",
            },
        )
        if payload.get("contract_version") != OWNER_CONTEXT_ACTION_VERSION:
            raise ContractError("unsupported_contract_version")
        action = str(payload.get("action") or "")
        if action not in OWNER_CONTEXT_ACTIONS:
            raise ContractError("invalid_owner_action")
        if payload.get("confirmation_action") != OWNER_CONTEXT_ACTION_CONFIRMATION:
            raise ContractError("explicit_owner_confirmation_required")
        idempotency_key = str(payload.get("idempotency_key") or "")
        _validate_opaque_ref("idempotency_key", idempotency_key)
        normalized: dict[str, Any] = {
            key: payload.get(key, "")
            for key in _OWNER_CONTEXT_ACTION_FIELDS
        }
        normalized.update(
            {
                "contract_version": OWNER_CONTEXT_ACTION_VERSION,
                "action": action,
                "idempotency_key": idempotency_key,
                "confirmation_action": OWNER_CONTEXT_ACTION_CONFIRMATION,
            }
        )
        answer = str(payload.get("answer") or "")
        if action == "confirm_card":
            confirmation_ref = str(payload.get("confirmation_ref") or "")
            _validate_opaque_ref("confirmation_ref", confirmation_ref)
            expected_revision = str(payload.get("expected_revision") or "")
            if not expected_revision or _contains_unicode_control(expected_revision):
                raise ContractError("confirmation_revision_required")
            if answer not in OWNER_CONTEXT_ANSWERS:
                raise ContractError("invalid_owner_answer")
            normalized["confirmation_ref"] = confirmation_ref
            normalized["expected_revision"] = expected_revision
            normalized["answer"] = answer
            normalized["reason"] = str(payload.get("reason") or "other")
            # The server, not Swift, is authoritative for all Context refs and
            # classification derived from a confirmation card. Ignore legacy
            # clients that still submit those fields so they cannot spoof a
            # project, owner subject, privacy class, or completion state.
            for field in (
                "workstream",
                "outcome",
                "project_ref",
                "workstream_ref",
                "object_ref",
                "version_ref",
                "subject_ref",
                "privacy_class",
                "context_state",
            ):
                normalized[field] = ""
        else:
            confirmation_ref = ""
            normalized["safe_label"] = _validate_manual_note_label(
                payload.get("safe_label")
            )
            workstream = str(payload.get("workstream") or "")
            if workstream:
                workstream = _validate_approved_label(workstream)
            normalized["workstream"] = workstream
            outcome = str(payload.get("outcome") or "unknown")
            if outcome not in {
                "completed",
                "blocked",
                "progressing",
                "preparation_only",
                "unknown",
            }:
                raise ContractError("invalid_manual_outcome")
            normalized["outcome"] = outcome
            started = _parse_contract_time(str(payload.get("started_at") or ""))
            ended = _parse_contract_time(
                str(payload.get("ended_at") or payload.get("started_at") or "")
            )
            if ended < started or ended - started > timedelta(days=1):
                raise ContractError("invalid_manual_time_range")
            if started > current + RECEIPT_FUTURE_SKEW:
                raise ContractError("manual_note_from_future")
            normalized["started_at"] = _iso(started)
            normalized["ended_at"] = _iso(ended)
            project_ref = str(payload.get("project_ref") or "")
            if project_ref:
                _validate_ref_for_kind("project", project_ref)
            project_label = str(payload.get("project_label") or "")
            if project_label:
                project_label = _validate_approved_label(project_label)
            normalized["project_ref"] = project_ref
            normalized["project_label"] = project_label
            # Core is authoritative for all remaining graph fields, owner
            # identity, privacy class, and state derived from outcome.
            for field in (
                "workstream_ref",
                "object_ref",
                "version_ref",
                "subject_ref",
                "privacy_class",
                "context_state",
            ):
                normalized[field] = ""
            answer = "occurred"
            normalized["answer"] = answer
        verifying = answer == "occurred"
        action_payload_hash = _payload_hash(normalized)
    except ContractError as exc:
        return _owner_action_result(status="rejected", reason_code=exc.reason_code)

    if not context_console_enabled(conn):
        return _owner_action_result(status="disabled", reason_code="feature_flag_off")

    try:
        with _context_authorization_lock(conn, exclusive=False):
            return _record_owner_context_action_locked(
                conn,
                action=action,
                idempotency_key=idempotency_key,
                confirmation_ref=confirmation_ref,
                answer=answer,
                verifying=verifying,
                normalized=normalized,
                action_payload_hash=action_payload_hash,
                current=current,
            )
    except ContractError as exc:
        if conn.in_transaction:
            conn.rollback()
        status = (
            "conflict"
            if exc.reason_code
            in {
                "idempotency_conflict",
                "confirmation_action_conflict",
                "confirmation_already_linked",
            }
            else "rejected"
        )
        return _owner_action_result(status=status, reason_code=exc.reason_code)
    except sqlite3.IntegrityError:
        if conn.in_transaction:
            conn.rollback()
        return _owner_action_result(
            status="conflict",
            reason_code="concurrent_owner_action_conflict",
        )
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _owner_action_result(
            status="failed",
            reason_code="database_write_failed",
        )
    except BaseException:
        # This public boundary owns the transaction opened by
        # _record_owner_context_action_locked. Process-control exceptions must
        # not leave a partial legacy confirmation/context graph that a later,
        # unrelated commit could persist.
        _rollback_owner_action_or_discard_connection(conn)
        raise


def _product_feedback_result(
    *,
    status: str,
    reason_code: str = "",
    feedback_ref: str | None = None,
    source_kind: str | None = None,
    source_ref: str | None = None,
    useful: bool | None = None,
    win_recorded: bool = False,
    experiment_recorded: bool = False,
) -> dict[str, Any]:
    return {
        "contract_version": PRODUCT_FEEDBACK_VERSION,
        "status": status,
        "feedback_ref": feedback_ref,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "useful": useful,
        "win_recorded": win_recorded,
        "experiment_recorded": experiment_recorded,
        "raw_access": False,
    }


def _feedback_experiment_locked(
    conn: sqlite3.Connection,
    *,
    experiment_ref: str,
    feedback_ref: str,
    source_ref: str,
    useful: bool,
    resume_seconds: float | None,
    current: datetime,
) -> tuple[str, bool, bool]:
    running = conn.execute(
        "SELECT * FROM context_product_experiments WHERE status='running' "
        "ORDER BY created_at DESC",
    ).fetchall()
    if len(running) > 1:
        raise ContractError("multiple_running_experiments")
    if not running:
        if experiment_ref:
            experiment = conn.execute(
                "SELECT status FROM context_product_experiments WHERE experiment_ref=?",
                (experiment_ref,),
            ).fetchone()
            if not experiment:
                raise ContractError("experiment_not_found")
            raise ContractError("experiment_not_running")
        return "", False, False
    experiment = running[0]
    active_ref = str(experiment["experiment_ref"] or "")
    if not experiment_ref:
        raise ContractError("experiment_ref_required")
    if not hmac.compare_digest(active_ref, experiment_ref):
        raise ContractError("experiment_ref_mismatch")
    if resume_seconds is None:
        raise ContractError("resume_seconds_required")
    selected_ref = str(experiment["experiment_ref"])
    try:
        started = _canonical_product_experiment_date(
            experiment["started_local_date"],
            "invalid_experiment_date",
        )
        minimum = int(experiment["minimum_days"])
        maximum = int(experiment["maximum_days"])
        if (
            minimum != PRODUCT_EXPERIMENT_MIN_DAYS
            or maximum != PRODUCT_EXPERIMENT_MAX_DAYS
        ):
            raise ContractError("invalid_experiment_window")
        timezone_name = _experiment_timezone_name(experiment["timezone_name"])
    except (TypeError, ValueError) as exc:
        raise ContractError("invalid_experiment_window") from exc
    local_day = _local_today(conn, now=current, timezone_name=timezone_name)
    maximum_date = started + timedelta(days=maximum - 1)
    if local_day < started:
        raise ContractError("attempt_before_experiment")
    if local_day > maximum_date:
        raise ContractError("attempt_after_experiment_window")
    terminal = _finalize_product_experiment_after_write_locked(
        conn,
        experiment_ref=selected_ref,
        evaluated_local_date=local_day,
        current=current,
    )
    if terminal is not None:
        return selected_ref, False, True
    event_id = _derived_ref("event", f"feedback|{feedback_ref}")
    attempt_ref = _derived_ref("attempt", f"{selected_ref}|{event_id}")
    experiment_source_ref = _derived_ref(
        "source",
        f"product-feedback|{source_ref}",
    )
    conn.execute(
        "INSERT INTO context_product_experiment_attempts(attempt_ref,experiment_ref,event_id,local_date,useful,"
        "outcome,resume_seconds,source_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            attempt_ref,
            selected_ref,
            event_id,
            local_day.isoformat(),
            int(useful),
            "useful" if useful else "not_useful",
            resume_seconds,
            experiment_source_ref,
            _iso(current),
        ),
    )
    _finalize_product_experiment_after_write_locked(
        conn,
        experiment_ref=selected_ref,
        evaluated_local_date=local_day,
        current=current,
    )
    return selected_ref, True, False


def record_product_feedback(
    conn: sqlite3.Connection,
    raw_payload: bytes | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    # An injected timestamp is an explicit deterministic contract for tests and
    # controlled replay.  Production calls must take their trust timestamp only
    # after waiting for the authorization lease and SQLite write transaction;
    # otherwise a bundle can expire while the caller is blocked and still be
    # accepted using the stale pre-wait time.
    injected_current = _aware_utc(now) if now is not None else None
    try:
        payload = _parse_closed_json(
            raw_payload,
            _PRODUCT_FEEDBACK_FIELDS,
            required_fields={
                "contract_version",
                "idempotency_key",
                "source_kind",
                "source_ref",
                "project_ref",
                "useful",
            },
        )
        if payload.get("contract_version") != PRODUCT_FEEDBACK_VERSION:
            raise ContractError("unsupported_contract_version")
        idempotency_key = str(payload.get("idempotency_key") or "")
        _validate_opaque_ref("idempotency_key", idempotency_key)
        source_kind = str(payload.get("source_kind") or "")
        if source_kind not in PRODUCT_FEEDBACK_SOURCE_KINDS:
            raise ContractError("invalid_feedback_source_kind")
        source_ref = str(payload.get("source_ref") or "")
        _validate_opaque_ref("bundle_ref", source_ref)
        project_ref = str(payload.get("project_ref") or "")
        _validate_opaque_ref("project_ref", project_ref)
        useful = payload.get("useful")
        if not isinstance(useful, bool):
            raise ContractError("feedback_useful_must_be_boolean")
        resume_raw = payload.get("resume_seconds")
        if resume_raw is None:
            resume_seconds = None
        else:
            if isinstance(resume_raw, bool):
                raise ContractError("invalid_resume_seconds")
            resume_seconds = float(resume_raw)
            if (
                not math.isfinite(resume_seconds)
                or resume_seconds < 0
                or resume_seconds > 86_400
            ):
                raise ContractError("invalid_resume_seconds")
        experiment_ref = str(payload.get("experiment_ref") or "")
        if experiment_ref:
            _validate_opaque_ref("experiment_ref", experiment_ref)
        normalized = {
            "contract_version": PRODUCT_FEEDBACK_VERSION,
            "idempotency_key": idempotency_key,
            "source_kind": source_kind,
            "source_ref": source_ref,
            "project_ref": project_ref,
            "useful": useful,
            "resume_seconds": resume_seconds,
            "experiment_ref": experiment_ref,
        }
        payload_hash = _payload_hash(normalized)
    except (ContractError, TypeError, ValueError) as exc:
        reason = exc.reason_code if isinstance(exc, ContractError) else "invalid_product_feedback"
        return _product_feedback_result(status="rejected", reason_code=reason)

    if not context_console_enabled(conn):
        return _product_feedback_result(
            status="disabled",
            reason_code="feature_flag_off",
        )
    try:
        with _context_authorization_lock(conn, exclusive=False):
            conn.execute("BEGIN IMMEDIATE")
            current = injected_current if injected_current is not None else _aware_utc(None)
            # The flag can be disabled while this writer waits for the shared
            # authorization lease or SQLite transaction.  Recheck inside the
            # transaction so an in-flight request cannot write after shutdown.
            if not context_console_enabled(conn):
                raise ContractError("feature_flag_off")
            idempotency_existing = conn.execute(
                "SELECT * FROM context_product_feedback WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if idempotency_existing:
                if hmac.compare_digest(
                    str(idempotency_existing["payload_hash"] or ""),
                    payload_hash,
                ):
                    conn.rollback()
                    return _product_feedback_result(
                        status="replayed",
                        reason_code="idempotent_replay",
                        feedback_ref=str(idempotency_existing["feedback_ref"]),
                        source_kind=source_kind,
                        source_ref=source_ref,
                        useful=bool(idempotency_existing["useful"]),
                        win_recorded=bool(idempotency_existing["win_ref"]),
                        experiment_recorded=bool(idempotency_existing["experiment_ref"]),
                    )
                raise ContractError("product_feedback_conflict")

            source_existing = conn.execute(
                "SELECT * FROM context_product_feedback WHERE source_ref=?",
                (source_ref,),
            ).fetchone()

            _require_active_label(conn, project_ref, "project")
            manifest = conn.execute(
                "SELECT * FROM context_bundle_manifests WHERE bundle_ref=?",
                (source_ref,),
            ).fetchone()
            if not manifest:
                raise ContractError("feedback_source_not_found")
            if str(manifest["project_ref"] or "") != project_ref:
                raise ContractError("feedback_project_mismatch")
            _validate_bundle_manifest_live(conn, manifest, current)
            expected_handoff = _derived_ref(
                "bundle",
                f"local_handoff|{project_ref}|{manifest['bundle_hash']}",
            )
            is_local_handoff = hmac.compare_digest(expected_handoff, source_ref)
            if source_kind == "local_handoff":
                if not is_local_handoff or str(manifest["target_ref"] or ""):
                    raise ContractError("feedback_source_kind_mismatch")
            elif is_local_handoff or not str(manifest["target_ref"] or ""):
                raise ContractError("feedback_source_kind_mismatch")
            if source_existing:
                raise ContractError("product_feedback_conflict")

            local_date, expires_local_date, expires_at, timezone_name = _local_retention_window(
                conn,
                current,
                PRODUCT_WIN_RETENTION_DAYS,
            )
            feedback_ref = _derived_ref(
                "feedback",
                f"{source_kind}|{source_ref}|{idempotency_key}|{payload_hash}",
            )
            (
                effective_experiment_ref,
                experiment_recorded,
                terminal_reached,
            ) = _feedback_experiment_locked(
                conn,
                experiment_ref=experiment_ref,
                feedback_ref=feedback_ref,
                source_ref=source_ref,
                useful=bool(useful),
                resume_seconds=resume_seconds,
                current=current,
            )
            if terminal_reached:
                conn.commit()
                return _product_feedback_result(
                    status="rejected",
                    reason_code="experiment_not_running",
                )
            win_ref = ""
            if useful:
                win_ref = _derived_ref("win", f"product-feedback|{feedback_ref}")
                win_source_ref = _derived_ref(
                    "source",
                    f"product-feedback|{feedback_ref}",
                )
                conn.execute(
                    "INSERT INTO context_product_wins(win_ref,occurred_at,local_date,project_ref,outcome_code,"
                    "minutes_saved,source_ref,created_at,expires_local_date,timezone_name,expires_at) "
                    "VALUES(?,?,?,?, 'recovered_and_resumed',0,?,?,?,?,?)",
                    (
                        win_ref,
                        _iso(current),
                        local_date,
                        project_ref,
                        win_source_ref,
                        _iso(current),
                        expires_local_date,
                        timezone_name,
                        expires_at,
                    ),
                )
            conn.execute(
                "INSERT INTO context_product_feedback(feedback_ref,source_kind,source_ref,bundle_ref,bundle_hash,"
                "project_ref,idempotency_key,payload_hash,useful,resume_seconds,experiment_ref,win_ref,occurred_at,"
                "local_date,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    feedback_ref,
                    source_kind,
                    source_ref,
                    source_ref,
                    str(manifest["bundle_hash"]),
                    project_ref,
                    idempotency_key,
                    payload_hash,
                    int(bool(useful)),
                    resume_seconds,
                    effective_experiment_ref,
                    win_ref,
                    _iso(current),
                    local_date,
                    _iso(current),
                ),
            )
            conn.commit()
        return _product_feedback_result(
            status="recorded",
            feedback_ref=feedback_ref,
            source_kind=source_kind,
            source_ref=source_ref,
            useful=bool(useful),
            win_recorded=bool(win_ref),
            experiment_recorded=experiment_recorded,
        )
    except ContractError as exc:
        if conn.in_transaction:
            conn.rollback()
        if exc.reason_code == "feature_flag_off":
            status = "disabled"
        else:
            status = "conflict" if exc.reason_code == "product_feedback_conflict" else "rejected"
        return _product_feedback_result(status=status, reason_code=exc.reason_code)
    except sqlite3.IntegrityError:
        if conn.in_transaction:
            conn.rollback()
        return _product_feedback_result(
            status="conflict",
            reason_code="concurrent_product_feedback_conflict",
        )
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _product_feedback_result(
            status="failed",
            reason_code="database_write_failed",
        )
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def report_context_bundle_adapter(
    conn: sqlite3.Connection,
    *,
    start_at: str,
    end_at: str,
) -> dict[str, Any]:
    """Safe contract-v2 adapter projection: verified data only, no clue body."""
    bundle = build_context_bundle(
        conn,
        start_at=start_at,
        end_at=end_at,
        include_clues=False,
        limit=100,
    )
    if bundle.get("status") != "available":
        return {
            "contract_version": CONTEXT_BUNDLE_VERSION,
            "status": str(bundle.get("status") or "unavailable"),
            "raw_access": False,
            "verified_contexts": [],
            "unverified_clues": [],
        }
    allowed_items = []
    for item in bundle.get("verified_contexts") or []:
        allowed_items.append(
            {
                "context_ref": item["context_ref"],
                "source_ref": item["source_ref"],
                "state": item["state"],
                "occurred_at": item["occurred_at"],
                "expires_at": item["expires_at"],
                "revocation_state": item["revocation_state"],
                "privacy_class": item["privacy_class"],
                "project": item["project"],
                "workstream": item["workstream"],
                "object": item["object"],
                "version": item["version"],
                "subject": item["subject"],
            }
        )
    return {
        "contract_version": CONTEXT_BUNDLE_VERSION,
        "status": "available",
        "bundle_hash": bundle["bundle_hash"],
        "raw_access": False,
        "verified_contexts": allowed_items,
        "unverified_clues": [],
    }


def document_trusted_context_adapter(
    conn: sqlite3.Connection,
    *,
    start_at: str,
    end_at: str,
    limit: int = 100,
) -> dict[str, Any]:
    """Verified contexts whose live provenance graph includes a Document source version."""
    bundle = build_context_bundle(
        conn,
        start_at=start_at,
        end_at=end_at,
        include_clues=False,
        document_dependency_only=True,
        limit=limit,
    )
    if bundle.get("status") != "available":
        return {
            "contract_version": DOCUMENT_TRUSTED_PROJECTION_VERSION,
            "status": str(bundle.get("status") or "unavailable"),
            "raw_access": False,
            "verified_contexts": [],
            "unverified_clues": [],
        }
    allowed_items = []
    for item in bundle.get("verified_contexts") or []:
        allowed_items.append(
            {
                "context_ref": item["context_ref"],
                "source_ref": item["source_ref"],
                "state": item["state"],
                "occurred_at": item["occurred_at"],
                "expires_at": item["expires_at"],
                "revocation_state": item["revocation_state"],
                "privacy_class": item["privacy_class"],
                "project": item["project"],
                "workstream": item["workstream"],
                "object": item["object"],
                "version": item["version"],
                "subject": item["subject"],
            }
        )
    stable_projection = {
        "contract_version": DOCUMENT_TRUSTED_PROJECTION_VERSION,
        "raw_access": False,
        "verified_contexts": allowed_items,
    }
    return {
        "contract_version": DOCUMENT_TRUSTED_PROJECTION_VERSION,
        "status": "available",
        "bundle_hash": _public_hash(stable_projection),
        "raw_access": False,
        "verified_contexts": allowed_items,
        "unverified_clues": [],
    }



# Public authorization wrappers ------------------------------------------------

def register_context_label(conn: sqlite3.Connection, **kwargs: Any) -> dict[str, Any]:
    with _context_authorization_lock(conn, exclusive=False):
        return _register_context_label_locked(conn, **kwargs)


def revoke_context_label(conn: sqlite3.Connection, ref: str) -> dict[str, Any]:
    with _context_authorization_lock(conn, exclusive=True):
        return _revoke_context_label_locked(conn, ref)


def register_context_principal(conn: sqlite3.Connection, **kwargs: Any) -> dict[str, Any]:
    with _context_authorization_lock(conn, exclusive=False):
        return _register_context_principal_locked(conn, **kwargs)


def revoke_context_principal(conn: sqlite3.Connection, principal_id: str, key_id: str) -> dict[str, Any]:
    with _context_authorization_lock(conn, exclusive=True):
        return _revoke_context_principal_locked(conn, principal_id, key_id)


def ingest_context_receipt(
    conn: sqlite3.Connection,
    raw_contract: bytes | str,
    *,
    verification_secret: bytes | str,
    now: datetime | None = None,
) -> dict[str, Any]:
    return _ingest_context_receipt_locked(
        conn, raw_contract, verification_secret=verification_secret, now=now
    )


def ingest_controlled_evidence(
    conn: sqlite3.Connection,
    raw_contract: bytes | str,
    *,
    verification_secret: bytes | str,
    now: datetime | None = None,
) -> dict[str, Any]:
    return _ingest_controlled_evidence_locked(
        conn, raw_contract, verification_secret=verification_secret, now=now
    )


def build_context_bundle(conn: sqlite3.Connection, **kwargs: Any) -> dict[str, Any]:
    with _context_authorization_lock(conn, exclusive=False):
        return _build_context_bundle_locked(conn, **kwargs)


def create_memory_candidate(conn: sqlite3.Connection, **kwargs: Any) -> dict[str, Any]:
    with _context_authorization_lock(conn, exclusive=False):
        return _create_memory_candidate_locked(conn, **kwargs)


def prepare_delivery(conn: sqlite3.Connection, **kwargs: Any) -> dict[str, Any]:
    with _context_authorization_lock(conn, exclusive=False):
        return _prepare_delivery_locked(conn, **kwargs)


def ingest_delivery_receipt(
    conn: sqlite3.Connection,
    raw_contract: bytes | str,
    *,
    verification_secret: bytes | str,
    now: datetime | None = None,
) -> dict[str, Any]:
    return _ingest_delivery_receipt_locked(
        conn, raw_contract, verification_secret=verification_secret, now=now
    )


def delete_context_observation(conn: sqlite3.Connection, **kwargs: Any) -> dict[str, Any]:
    with _context_authorization_lock(conn, exclusive=True):
        return _delete_context_observation_locked(conn, **kwargs)


def revoke_context_observations_for_range(
    conn: sqlite3.Connection,
    start_at: str | None = None,
    end_at: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if start_at is not None:
        kwargs.setdefault("start_at", start_at)
    if end_at is not None:
        kwargs.setdefault("end_at", end_at)
    with _context_authorization_lock(conn, exclusive=True):
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = _revoke_context_observations_for_range_locked(conn, **kwargs)
            conn.commit()
            return result
        except ContractError as exc:
            if conn.in_transaction:
                conn.rollback()
            return _result("rejected", exc.reason_code)
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
            return _result("failed", "deletion_cascade_failed")
        except BaseException:
            # Do not turn programmer/runtime failures into a public success,
            # but never release the authorization lease with a partial delete
            # transaction that a later caller could accidentally commit.
            if conn.in_transaction:
                conn.rollback()
            raise


def cleanup_context_retention(conn: sqlite3.Connection, **kwargs: Any) -> dict[str, Any]:
    with _context_authorization_lock(conn, exclusive=True):
        return _cleanup_context_retention_locked(conn, **kwargs)


def _canonical_source_event_ref(contract_type: str, principal_id: str, payload: Mapping[str, Any]) -> str:
    """Stable logical source identity.

    Transport/authentication fields, timestamps, and mutable business binding
    fields cannot mint a second identity for the same governed source event.
    """
    return _derived_ref(
        "sourceevent",
        f"{contract_type}|{principal_id}|{str(payload.get('source_ref') or '')}",
    )


def _source_event_semantic_hash(contract_type: str, payload: Mapping[str, Any]) -> str:
    excluded = {
        "signature",
        "idempotency_key",
        "key_id",
        "producer_id",
        "principal_id",
        "occurred_at",
    }
    semantic = {key: value for key, value in payload.items() if key not in excluded}
    semantic["contract_type"] = contract_type
    return hashlib.sha256(_canonical_bytes(semantic)).hexdigest()


def _external_receipt_binding(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        field: str(payload.get(field) or "")
        for field in (
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


def _bind_external_receipt_locked(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    payload: Mapping[str, Any],
    current: datetime,
) -> None:
    provider_receipt_ref = str(payload["provider_receipt_ref"])
    binding = _external_receipt_binding(payload)
    semantic_hash = hashlib.sha256(_canonical_bytes(binding)).hexdigest()
    existing_rows = conn.execute(
        "SELECT semantic_hash,source_ref,project_ref,workstream_ref,object_ref,version_ref,"
        "subject_ref,privacy_class,state FROM context_external_receipt_bindings "
        "WHERE principal_id=? AND provider_receipt_ref=?",
        (principal_id, provider_receipt_ref),
    ).fetchall()
    if len(existing_rows) > 1:
        raise ContractError("external_receipt_binding_conflict")
    if existing_rows:
        existing = existing_rows[0]
        stored_binding = {
            field: str(existing[field] or "")
            for field in (
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
        stored_hash = hashlib.sha256(_canonical_bytes(stored_binding)).hexdigest()
        if (
            not hmac.compare_digest(str(existing["semantic_hash"] or ""), stored_hash)
            or not hmac.compare_digest(stored_hash, semantic_hash)
        ):
            raise ContractError("external_receipt_binding_conflict")
        return
    conn.execute(
        "INSERT INTO context_external_receipt_bindings("
        "principal_id,provider_receipt_ref,semantic_hash,source_ref,project_ref,workstream_ref,"
        "object_ref,version_ref,subject_ref,privacy_class,state,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            principal_id,
            provider_receipt_ref,
            semantic_hash,
            binding["source_ref"],
            binding["project_ref"],
            binding["workstream_ref"],
            binding["object_ref"],
            binding["version_ref"],
            binding["subject_ref"],
            binding["privacy_class"],
            binding["state"],
            _iso(current),
        ),
    )


def _backfill_external_receipt_bindings(conn: sqlite3.Connection) -> None:
    if not _table_columns(conn, "context_evidence"):
        return
    rows = conn.execute(
        "SELECT principal_id,provenance_ref,source_ref,project_ref,workstream_ref,object_ref,"
        "version_ref,subject_ref,privacy_class,state FROM context_evidence "
        "WHERE evidence_kind='external_receipt' "
        "ORDER BY datetime(created_at),evidence_ref"
    ).fetchall()
    if not rows:
        return
    conn.execute("SAVEPOINT context_external_receipt_backfill")
    try:
        current = _aware_utc(None)
        for row in rows:
            provider_receipt_ref = str(row["provenance_ref"] or "")
            try:
                _validate_opaque_ref("provider_receipt_ref", provider_receipt_ref)
                _bind_external_receipt_locked(
                    conn,
                    principal_id=str(row["principal_id"] or ""),
                    payload={
                        "provider_receipt_ref": provider_receipt_ref,
                        "source_ref": row["source_ref"],
                        "project_ref": row["project_ref"],
                        "workstream_ref": row["workstream_ref"],
                        "object_ref": row["object_ref"],
                        "version_ref": row["version_ref"],
                        "subject_ref": row["subject_ref"],
                        "privacy_class": row["privacy_class"],
                        "state": row["state"],
                    },
                    current=current,
                )
            except ContractError as exc:
                raise sqlite3.DatabaseError(
                    "external_receipt_binding_history_conflict"
                ) from exc
        conn.execute("RELEASE SAVEPOINT context_external_receipt_backfill")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT context_external_receipt_backfill")
        conn.execute("RELEASE SAVEPOINT context_external_receipt_backfill")
        raise


def _source_event_guard_locked(
    conn: sqlite3.Connection,
    *,
    source_event_ref: str,
    semantic_hash: str,
) -> dict[str, str] | None:
    tombstone = conn.execute(
        "SELECT 1 FROM context_source_event_tombstones WHERE source_event_ref=?",
        (source_event_ref,),
    ).fetchone()
    if tombstone:
        raise ContractError("source_event_tombstoned")
    row = conn.execute(
        "SELECT payload_hash,observation_ref,status FROM context_source_events WHERE source_event_ref=?",
        (source_event_ref,),
    ).fetchone()
    if not row:
        return None
    if str(row["status"] or "") != "active":
        raise ContractError("source_event_tombstoned")
    semantic_match = hmac.compare_digest(str(row["payload_hash"] or ""), semantic_hash)
    receipt = conn.execute(
        "SELECT receipt_ref,result_status FROM context_ingest_receipts WHERE source_event_ref=? "
        "ORDER BY received_at,receipt_ref LIMIT 1",
        (source_event_ref,),
    ).fetchone()
    observation = conn.execute(
        "SELECT lifecycle_event FROM context_observations WHERE observation_ref=?",
        (str(row["observation_ref"] or ""),),
    ).fetchone()
    result_status = str(receipt["result_status"] or "") if receipt else ""
    if not result_status and observation:
        result_status = str(observation["lifecycle_event"] or "")
    return {
        "receipt_ref": str(receipt["receipt_ref"] or "") if receipt else "",
        "observation_ref": str(row["observation_ref"] or ""),
        "result_status": result_status or "replayed",
        "semantic_match": "1" if semantic_match else "0",
    }


def _insert_source_event_locked(
    conn: sqlite3.Connection,
    *,
    contract_type: str,
    principal: Principal,
    payload: Mapping[str, Any],
    semantic_hash: str,
    source_event_ref: str,
    observation_ref: str,
    current: datetime,
) -> None:
    conn.execute(
        "INSERT INTO context_source_events(source_event_ref,contract_type,principal_id,key_id,source_ref,occurred_at,"
        "project_ref,workstream_ref,object_ref,version_ref,subject_ref,payload_hash,observation_ref,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
        (
            source_event_ref,
            contract_type,
            principal.principal_id,
            principal.key_id,
            payload["source_ref"],
            payload["occurred_at"],
            payload["project_ref"],
            payload["workstream_ref"],
            payload["object_ref"],
            payload["version_ref"],
            payload["subject_ref"],
            semantic_hash,
            observation_ref,
            _iso(current),
        ),
    )


def _insert_ingest_receipt_locked(
    conn: sqlite3.Connection,
    *,
    normalized: Mapping[str, Any],
    principal: Principal,
    contract_type: str,
    payload_hash: str,
    receipt_ref: str,
    source_event_ref: str,
    observation_ref: str,
    result_status: str,
    current: datetime,
) -> None:
    conn.execute(
        "INSERT INTO context_ingest_receipts(receipt_ref,contract_type,principal_id,key_id,source_ref,idempotency_key,payload_hash,"
        "source_event_ref,received_at,occurred_at,project_ref,observation_ref,result_status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            receipt_ref,
            contract_type,
            principal.principal_id,
            principal.key_id,
            normalized["source_ref"],
            normalized["idempotency_key"],
            payload_hash,
            source_event_ref,
            _iso(current),
            normalized["occurred_at"],
            normalized["project_ref"],
            observation_ref,
            result_status,
        ),
    )


def _scoped_ingest_idempotency_locked(
    conn: sqlite3.Connection,
    *,
    contract_type: str,
    principal_id: str,
    source_ref: str,
    idempotency_key: str,
    payload_hash: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload_hash,receipt_ref,observation_ref,result_status,source_event_ref "
        "FROM context_ingest_receipts WHERE contract_type=? AND principal_id=? "
        "AND source_ref=? AND idempotency_key=?",
        (contract_type, principal_id, source_ref, idempotency_key),
    ).fetchone()
    if not row:
        return None
    if not hmac.compare_digest(str(row["payload_hash"] or ""), payload_hash):
        raise ContractError("idempotency_conflict")
    return {
        "status": "replayed",
        "reason_code": "idempotent_replay",
        "receipt_ref": str(row["receipt_ref"] or ""),
        "observation_ref": str(row["observation_ref"] or ""),
        "result_status": str(row["result_status"] or ""),
        "source_event_ref": str(row["source_event_ref"] or ""),
        "raw_access": False,
    }


def _scoped_ingest_race_result(
    conn: sqlite3.Connection,
    *,
    contract_type: str,
    principal_id: str,
    source_ref: str,
    idempotency_key: str,
    payload_hash: str,
) -> dict[str, Any]:
    try:
        replay = _scoped_ingest_idempotency_locked(
            conn,
            contract_type=contract_type,
            principal_id=principal_id,
            source_ref=source_ref,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
        return replay or _result("failed", "concurrent_write_conflict")
    except ContractError as exc:
        return _result("conflict", exc.reason_code)

# ---------------------------------------------------------------------------
# Validation and materialization helpers
# ---------------------------------------------------------------------------


def _validate_context_receipt(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("contract_version") != CONTEXT_RECEIPT_VERSION:
        raise ContractError("unsupported_contract_version")
    normalized = _normalize_for_signature(payload)
    _validate_opaque_ref("idempotency_key", normalized.get("idempotency_key"))
    _validate_opaque_ref("producer_id", normalized.get("producer_id"))
    _validate_opaque_ref("key_id", normalized.get("key_id"))
    _validate_opaque_ref("source_ref", normalized.get("source_ref"))
    _parse_contract_time(normalized.get("occurred_at"))
    refs = {key: normalized.get(key) for key in ("project_ref", "workstream_ref", "object_ref", "version_ref", "subject_ref")}
    _validate_registry_refs(conn, refs)
    if normalized.get("privacy_class") not in PRIVACY_CLASSES:
        raise ContractError("invalid_privacy_class")
    if normalized.get("lifecycle_event") not in LIFECYCLE_EVENTS:
        raise ContractError("invalid_lifecycle_event")
    evidence_refs = normalized.get("evidence_refs")
    if not isinstance(evidence_refs, list):
        raise ContractError("evidence_refs_must_be_list")
    if len(evidence_refs) > MAX_EVIDENCE_REFS:
        raise ContractError("too_many_evidence_refs")
    deduped: list[str] = []
    for ref in evidence_refs:
        _validate_opaque_ref("evidence_ref", ref)
        if ref in deduped:
            raise ContractError("duplicate_evidence_ref")
        deduped.append(ref)
    normalized["evidence_refs"] = sorted(deduped)
    if not isinstance(normalized.get("signature"), str) or not _SIGNATURE_RE.fullmatch(normalized["signature"]):
        raise ContractError("invalid_signature_format")
    return normalized


def _validate_controlled_evidence(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("contract_version") != CONTROLLED_EVIDENCE_VERSION:
        raise ContractError("unsupported_contract_version")
    normalized = _normalize_for_signature(payload)
    for field in ("idempotency_key", "principal_id", "key_id", "source_ref"):
        _validate_opaque_ref(field, normalized.get(field))
    _parse_contract_time(normalized.get("occurred_at"))
    refs = {key: normalized.get(key) for key in ("project_ref", "workstream_ref", "object_ref", "version_ref", "subject_ref")}
    _validate_registry_refs(conn, refs)
    if normalized.get("privacy_class") not in PRIVACY_CLASSES:
        raise ContractError("invalid_privacy_class")
    evidence_kind = normalized.get("evidence_kind")
    state = normalized.get("state")
    if evidence_kind not in CONTROLLED_EVIDENCE_KINDS:
        raise ContractError("invalid_evidence_kind")
    if state not in CONTEXT_STATES:
        raise ContractError("invalid_context_state")
    allowed_states = {
        "user_confirmation": CONTEXT_STATES,
        "file_saved": {"artifact_saved"},
        "git_commit": {"artifact_committed"},
        "external_receipt": {"external_delivered", "completed_confirmed"},
    }[evidence_kind]
    if state not in allowed_states:
        raise ContractError("evidence_state_exceeds_adapter_cap")
    provider_ref = normalized.get("provider_receipt_ref")
    if evidence_kind == "external_receipt":
        _validate_opaque_ref("provider_receipt_ref", provider_ref)
    elif provider_ref != "":
        raise ContractError("provider_receipt_not_allowed")
    verification_scope = normalized.get("verification_scope")
    memory_eligible = normalized.get("memory_eligible")
    if verification_scope not in {"operational", "memory"}:
        raise ContractError("invalid_verification_scope")
    if not isinstance(memory_eligible, bool):
        raise ContractError("memory_eligible_must_be_boolean")
    subject = _require_active_label(conn, str(normalized.get("subject_ref") or ""), "subject")
    if memory_eligible:
        if evidence_kind != "user_confirmation" or verification_scope != "memory":
            raise ContractError("memory_scope_not_authorized")
        if str(subject["subject_scope"] or "") != "owner":
            raise ContractError("third_party_subject_forbidden")
        if normalized.get("privacy_class") == "restricted_local":
            raise ContractError("restricted_local_memory_forbidden")
    elif verification_scope != "operational":
        raise ContractError("memory_scope_requires_eligibility")
    if evidence_kind != "user_confirmation" and (memory_eligible or verification_scope != "operational"):
        raise ContractError("operational_evidence_memory_forbidden")
    if not isinstance(normalized.get("signature"), str) or not _SIGNATURE_RE.fullmatch(normalized["signature"]):
        raise ContractError("invalid_signature_format")
    return normalized


def _validate_delivery_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("contract_version") != DELIVERY_RECEIPT_VERSION:
        raise ContractError("unsupported_contract_version")
    normalized = _normalize_for_signature(payload)
    for field in (
        "idempotency_key",
        "principal_id",
        "key_id",
        "delivery_ref",
        "bundle_ref",
        "project_ref",
        "target_ref",
    ):
        _validate_opaque_ref(field, normalized.get(field))
    attempt_no = normalized.get("attempt_no")
    if isinstance(attempt_no, bool) or not isinstance(attempt_no, int) or attempt_no < 1:
        raise ContractError("invalid_delivery_attempt")
    _parse_contract_time(normalized.get("occurred_at"))
    for field in ("bundle_hash", "privacy_policy_hash"):
        value = normalized.get(field)
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise ContractError(f"invalid_{field}")
    target_scope = normalized.get("target_scope")
    export_policy = normalized.get("export_policy")
    if target_scope not in TARGET_SCOPES:
        raise ContractError("invalid_target_scope")
    if export_policy not in TARGET_EXPORT_POLICIES:
        raise ContractError("invalid_target_export_policy")
    if target_scope == "local" and export_policy != "local_only":
        raise ContractError("local_target_requires_local_only")
    if target_scope == "external" and export_policy != "explicit_export":
        raise ContractError("external_target_requires_explicit_export")
    principal_role = normalized.get("principal_role")
    if principal_role not in DELIVERY_PRINCIPAL_ROLES:
        raise ContractError("invalid_principal_role")
    transition = normalized.get("transition")
    if transition not in DELIVERY_TRANSITIONS:
        raise ContractError("invalid_delivery_transition")
    if transition == "consumed" and principal_role != "consumer":
        raise ContractError("principal_role_mismatch")
    if transition == "delivered" and principal_role != "provider":
        raise ContractError("principal_role_mismatch")
    provider_ref = normalized.get("provider_receipt_ref")
    failure_reason = normalized.get("failure_reason_code")
    if transition in {"consumed", "delivered"}:
        _validate_opaque_ref("provider_receipt_ref", provider_ref)
        if failure_reason != "":
            raise ContractError("failure_reason_not_allowed")
    else:
        if provider_ref != "":
            raise ContractError("provider_receipt_not_allowed")
        if failure_reason not in DELIVERY_FAILURE_REASONS:
            raise ContractError("invalid_failure_reason_code")
    if not isinstance(normalized.get("signature"), str) or not _SIGNATURE_RE.fullmatch(normalized["signature"]):
        raise ContractError("invalid_signature_format")
    return normalized

def _materialize_ai_receipt(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    payload_hash: str,
    current: datetime,
    principal: Principal,
    *,
    source_event_ref: str,
    receipt_ref: str,
) -> dict[str, str]:
    lifecycle = payload["lifecycle_event"]
    state_map = {
        "turn_started": "in_progress",
        "waiting_approval": "in_progress",
        "answer_ready": "draft_generated",
        "stop": "draft_generated",
        "idle": "draft_generated",
        "failed": "blocked",
        "interrupted": "blocked",
        "status_unknown": "information_insufficient",
    }
    state = state_map[lifecycle]
    observation_ref = _derived_ref("observation", source_event_ref)
    evidence_ref = _derived_ref("evidence", f"ai|{source_event_ref}")
    candidate_ref = _derived_ref("candidate", f"ai|{source_event_ref}")
    occurred = _parse_contract_time(payload["occurred_at"])
    expires_at = _unverified_expiry(conn, occurred)
    disposition = "information_insufficient" if state == "information_insufficient" else "candidate_only"
    conn.execute(
        "INSERT INTO context_observations(observation_ref,source_kind,source_ref,source_event_ref,principal_id,key_id,"
        "adapter_kind,receipt_ref,lifecycle_event,occurred_at,project_ref,workstream_ref,object_ref,version_ref,"
        "subject_ref,privacy_class,trust_partition,status,expires_at,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?)",
        (
            observation_ref,
            "ai_lifecycle",
            payload["source_ref"],
            source_event_ref,
            principal.principal_id,
            principal.key_id,
            principal.principal_kind,
            receipt_ref,
            lifecycle,
            payload["occurred_at"],
            payload["project_ref"],
            payload["workstream_ref"],
            payload["object_ref"],
            payload["version_ref"],
            payload["subject_ref"],
            payload["privacy_class"],
            "unverified_clue",
            expires_at,
            _iso(current),
        ),
    )
    conn.execute(
        "INSERT INTO context_evidence(evidence_ref,observation_ref,evidence_kind,evidence_tier,state,state_cap,provenance_ref,"
        "source_ref,occurred_at,project_ref,workstream_ref,object_ref,version_ref,subject_ref,principal_id,key_id,adapter_kind,"
        "privacy_class,status,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?)",
        (
            evidence_ref,
            observation_ref,
            "ai_lifecycle",
            "P2",
            state,
            "draft_generated",
            receipt_ref,
            payload["source_ref"],
            payload["occurred_at"],
            payload["project_ref"],
            payload["workstream_ref"],
            payload["object_ref"],
            payload["version_ref"],
            payload["subject_ref"],
            principal.principal_id,
            principal.key_id,
            principal.principal_kind,
            payload["privacy_class"],
            expires_at,
            _iso(current),
        ),
    )
    conn.execute(
        "INSERT INTO context_observation_evidence_links(observation_ref,evidence_ref) VALUES(?,?)",
        (observation_ref, evidence_ref),
    )
    for referenced in payload["evidence_refs"]:
        conn.execute(
            "INSERT INTO context_observation_evidence_links(observation_ref,evidence_ref) VALUES(?,?)",
            (observation_ref, referenced),
        )
    conn.execute(
        "INSERT INTO context_candidates(candidate_ref,observation_ref,desired_state,disposition,status,created_at,expires_at) "
        "VALUES(?,?,?,?, 'active',?,?)",
        (candidate_ref, observation_ref, state, disposition, _iso(current), expires_at),
    )
    conn.execute(
        "INSERT INTO context_candidate_evidence(candidate_ref,evidence_ref) VALUES(?,?)",
        (candidate_ref, evidence_ref),
    )
    for referenced in payload["evidence_refs"]:
        conn.execute(
            "INSERT INTO context_candidate_evidence(candidate_ref,evidence_ref) VALUES(?,?)",
            (candidate_ref, referenced),
        )
    _index_unverified_clue(conn, observation_ref)
    return {
        "receipt_ref": receipt_ref,
        "observation_ref": observation_ref,
        "evidence_ref": evidence_ref,
        "candidate_ref": candidate_ref,
        "verified_ref": "",
        "result_status": state,
        "verification_scope": "operational",
        "memory_eligible": False,
    }


def _operational_evidence_row(conn: sqlite3.Connection, evidence_ref: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT e.*,o.source_event_ref,o.trust_partition,o.status AS observation_status,"
        "c.candidate_ref,c.status AS candidate_status,c.disposition AS candidate_disposition,"
        "p.key_fingerprint,p.status AS principal_status FROM context_evidence e "
        "JOIN context_observations o ON o.observation_ref=e.observation_ref "
        "JOIN context_candidates c ON c.observation_ref=o.observation_ref "
        "JOIN context_principals p ON p.principal_id=e.principal_id AND p.key_id=e.key_id "
        "WHERE e.evidence_ref=? ORDER BY c.created_at,c.candidate_ref LIMIT 1",
        (evidence_ref,),
    ).fetchone()


def _operational_pair_is_independent(first: Mapping[str, Any], second: Mapping[str, Any], now: datetime) -> bool:
    if str(first["evidence_kind"] or "") not in OPERATIONAL_EVIDENCE_KINDS:
        return False
    if str(second["evidence_kind"] or "") not in OPERATIONAL_EVIDENCE_KINDS:
        return False
    for row in (first, second):
        if str(row["status"] or "") != "active":
            return False
        if str(row["observation_status"] or "") != "active":
            return False
        if str(row["candidate_status"] or "") != "active":
            return False
        if str(row["principal_status"] or "") != "active":
            return False
        if not str(row["provenance_ref"] or ""):
            return False
        if _parse_contract_time(str(row["expires_at"])) <= now:
            return False
    binding_fields = (
        "project_ref",
        "workstream_ref",
        "object_ref",
        "version_ref",
        "subject_ref",
        "privacy_class",
    )
    if any(str(first[field] or "") != str(second[field] or "") for field in binding_fields):
        return False
    first_time = _parse_contract_time(str(first["occurred_at"]))
    second_time = _parse_contract_time(str(second["occurred_at"]))
    if abs((first_time - second_time).total_seconds()) > EVIDENCE_BINDING_WINDOW.total_seconds():
        return False
    distinct_pairs = (
        ("adapter_kind", "independent_evidence_adapter_required"),
        ("principal_id", "independent_evidence_principal_required"),
        ("key_fingerprint", "independent_evidence_key_fingerprint_required"),
        ("source_ref", "independent_evidence_source_required"),
        ("source_event_ref", "independent_evidence_source_event_required"),
        ("provenance_ref", "independent_evidence_provenance_required"),
    )
    return all(str(first[field] or "") != str(second[field] or "") for field, _ in distinct_pairs)


def _promote_independent_operational_pair_locked(
    conn: sqlite3.Connection,
    *,
    candidate_ref: str,
    evidence_ref: str,
    now: datetime,
) -> dict[str, Any] | None:
    current = _operational_evidence_row(conn, evidence_ref)
    if not current:
        return None
    rows = conn.execute(
        "SELECT e.evidence_ref FROM context_evidence e "
        "WHERE e.evidence_ref<>? AND e.evidence_kind IN ('file_saved','git_commit') "
        "AND e.status='active' ORDER BY datetime(e.occurred_at),e.evidence_ref",
        (evidence_ref,),
    ).fetchall()
    for item in rows:
        other = _operational_evidence_row(conn, str(item["evidence_ref"]))
        if not other or not _operational_pair_is_independent(current, other, now):
            continue
        evidence_refs = sorted((str(current["evidence_ref"]), str(other["evidence_ref"])))
        verified_ref = _derived_ref("verified", "operational-pair|" + "|".join(evidence_refs))
        existing = conn.execute(
            "SELECT verified_ref FROM context_verified WHERE verified_ref=?", (verified_ref,)
        ).fetchone()
        if existing:
            return {
                "verified_ref": verified_ref,
                "state": str(conn.execute(
                    "SELECT state FROM context_verified WHERE verified_ref=?", (verified_ref,)
                ).fetchone()[0]),
            }
        state = "artifact_committed" if "artifact_committed" in {
            str(current["state"]), str(other["state"])
        } else "artifact_saved"
        valid_from = max(str(current["occurred_at"]), str(other["occurred_at"]))
        expiry = min(str(current["expires_at"]), str(other["expires_at"]))
        provenance_ref = _derived_ref(
            "provenance", "operational-pair|" + "|".join(sorted((
                str(current["provenance_ref"]), str(other["provenance_ref"])
            )))
        )
        pair_source_ref = _derived_ref(
            "source", "operational-pair|" + "|".join(sorted((
                str(current["source_ref"]), str(other["source_ref"])
            )))
        )
        observation_refs = (str(current["observation_ref"]), str(other["observation_ref"]))
        conn.execute(
            "UPDATE context_observations SET trust_partition='verified_eligible',expires_at=? "
            "WHERE observation_ref IN (?,?) AND status='active'",
            (expiry, *observation_refs),
        )
        for observation_ref in observation_refs:
            conn.execute(
                "DELETE FROM context_search_index WHERE verified_ref=?",
                (f"clue:{observation_ref}",),
            )
        conn.execute(
            "UPDATE context_candidates SET desired_state=?,disposition='verified',expires_at=? "
            "WHERE candidate_ref=? AND status='active'",
            (state, expiry, candidate_ref),
        )
        for ref in evidence_refs:
            conn.execute(
                "INSERT OR IGNORE INTO context_candidate_evidence(candidate_ref,evidence_ref) VALUES(?,?)",
                (candidate_ref, ref),
            )
        conn.execute(
            "INSERT INTO context_verified(verified_ref,candidate_ref,observation_ref,project_ref,workstream_ref,object_ref,"
            "version_ref,subject_ref,state,evidence_tier,source_kind,source_ref,provenance_ref,privacy_class,verification_scope,"
            "memory_eligible,valid_from,expires_at,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'P1','operational_corroborated',?,?,?,'operational',0,?,?,'active',?)",
            (
                verified_ref,
                candidate_ref,
                str(current["observation_ref"]),
                str(current["project_ref"]),
                str(current["workstream_ref"]),
                str(current["object_ref"]),
                str(current["version_ref"]),
                str(current["subject_ref"]),
                state,
                pair_source_ref,
                provenance_ref,
                str(current["privacy_class"]),
                valid_from,
                expiry,
                _iso(now),
            ),
        )
        _index_verified(conn, verified_ref)
        return {"verified_ref": verified_ref, "state": state}
    return None


def _materialize_controlled_evidence(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    payload_hash: str,
    current: datetime,
    principal: Principal,
    *,
    source_event_ref: str,
    receipt_ref: str,
) -> dict[str, Any]:
    observation_ref = _derived_ref("observation", source_event_ref)
    evidence_ref = _derived_ref("evidence", f"controlled|{source_event_ref}")
    candidate_ref = _derived_ref("candidate", f"controlled|{source_event_ref}")
    occurred = _parse_contract_time(payload["occurred_at"])
    verified_expiry = _verified_expiry(conn, occurred)
    candidate_expiry = _unverified_expiry(conn, occurred)
    evidence_kind = str(payload["evidence_kind"])
    direct_verified = evidence_kind in {"user_confirmation", "external_receipt"}
    expiry = verified_expiry if direct_verified else candidate_expiry
    evidence_tier = "P0" if direct_verified else "P1"
    state_cap = {
        "user_confirmation": "completed_confirmed",
        "file_saved": "artifact_saved",
        "git_commit": "artifact_committed",
        "external_receipt": "completed_confirmed",
    }[evidence_kind]
    provenance_ref = payload["provider_receipt_ref"] or receipt_ref
    _require_active_label(conn, payload["subject_ref"], "subject")
    memory_eligible = bool(payload["memory_eligible"])
    verification_scope = str(payload["verification_scope"])
    disposition = "verified" if direct_verified else "candidate_only"
    trust_partition = "verified_eligible" if direct_verified else "unverified_clue"
    conn.execute(
        "INSERT INTO context_observations(observation_ref,source_kind,source_ref,source_event_ref,principal_id,key_id,"
        "adapter_kind,receipt_ref,lifecycle_event,occurred_at,project_ref,workstream_ref,object_ref,version_ref,"
        "subject_ref,privacy_class,trust_partition,status,expires_at,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,'',?,?,?,?,?,?,?,?, 'active',?,?)",
        (
            observation_ref,
            principal.principal_kind,
            payload["source_ref"],
            source_event_ref,
            principal.principal_id,
            principal.key_id,
            principal.principal_kind,
            receipt_ref,
            payload["occurred_at"],
            payload["project_ref"],
            payload["workstream_ref"],
            payload["object_ref"],
            payload["version_ref"],
            payload["subject_ref"],
            payload["privacy_class"],
            trust_partition,
            expiry,
            _iso(current),
        ),
    )
    conn.execute(
        "INSERT INTO context_evidence(evidence_ref,observation_ref,evidence_kind,evidence_tier,state,state_cap,provenance_ref,"
        "source_ref,occurred_at,project_ref,workstream_ref,object_ref,version_ref,subject_ref,principal_id,key_id,adapter_kind,"
        "privacy_class,status,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?)",
        (
            evidence_ref,
            observation_ref,
            evidence_kind,
            evidence_tier,
            payload["state"],
            state_cap,
            provenance_ref,
            payload["source_ref"],
            payload["occurred_at"],
            payload["project_ref"],
            payload["workstream_ref"],
            payload["object_ref"],
            payload["version_ref"],
            payload["subject_ref"],
            principal.principal_id,
            principal.key_id,
            principal.principal_kind,
            payload["privacy_class"],
            expiry,
            _iso(current),
        ),
    )
    conn.execute(
        "INSERT INTO context_observation_evidence_links(observation_ref,evidence_ref) VALUES(?,?)",
        (observation_ref, evidence_ref),
    )
    conn.execute(
        "INSERT INTO context_candidates(candidate_ref,observation_ref,desired_state,disposition,status,created_at,expires_at) "
        "VALUES(?,?,?,?, 'active',?,?)",
        (candidate_ref, observation_ref, payload["state"], disposition, _iso(current), expiry),
    )
    conn.execute(
        "INSERT INTO context_candidate_evidence(candidate_ref,evidence_ref) VALUES(?,?)",
        (candidate_ref, evidence_ref),
    )
    verified_ref = ""
    result_state = str(payload["state"])
    if direct_verified:
        verified_ref = _derived_ref("verified", f"controlled|{source_event_ref}")
        conn.execute(
            "INSERT INTO context_verified(verified_ref,candidate_ref,observation_ref,project_ref,workstream_ref,object_ref,"
            "version_ref,subject_ref,state,evidence_tier,source_kind,source_ref,provenance_ref,privacy_class,verification_scope,"
            "memory_eligible,valid_from,expires_at,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
            (
                verified_ref,
                candidate_ref,
                observation_ref,
                payload["project_ref"],
                payload["workstream_ref"],
                payload["object_ref"],
                payload["version_ref"],
                payload["subject_ref"],
                payload["state"],
                evidence_tier,
                principal.principal_kind,
                payload["source_ref"],
                provenance_ref,
                payload["privacy_class"],
                verification_scope,
                1 if memory_eligible else 0,
                payload["occurred_at"],
                verified_expiry,
                _iso(current),
            ),
        )
        _index_verified(conn, verified_ref)
    else:
        promoted = _promote_independent_operational_pair_locked(
            conn, candidate_ref=candidate_ref, evidence_ref=evidence_ref, now=current
        )
        if promoted:
            verified_ref = str(promoted["verified_ref"])
            result_state = str(promoted["state"])
        else:
            _index_unverified_clue(conn, observation_ref)
    return {
        "receipt_ref": receipt_ref,
        "observation_ref": observation_ref,
        "evidence_ref": evidence_ref,
        "candidate_ref": candidate_ref,
        "verified_ref": verified_ref,
        "result_status": result_state,
        "memory_eligible": False if evidence_kind in OPERATIONAL_EVIDENCE_KINDS else memory_eligible,
        "verification_scope": "operational" if evidence_kind in OPERATIONAL_EVIDENCE_KINDS else verification_scope,
    }


def _select_verified_contexts(
    conn: sqlite3.Connection,
    *,
    project_ref: str,
    start_at: str,
    end_at: str,
    query: str,
    limit: int,
    now: datetime,
    document_dependency_only: bool = False,
) -> list[tuple[str, dict[str, Any]]]:
    params: list[Any] = [_iso(now), _iso(now), _iso(now), _iso(now)]
    where = [
        "v.status='active'",
        "v.provenance_ref<>''",
        "o.status='active'",
        "o.trust_partition='verified_eligible'",
        "o.source_kind NOT IN ('screen','ocr')",
        "c.status='active'",
        "c.disposition='verified'",
        "datetime(v.expires_at)>datetime(?)",
        "datetime(o.expires_at)>datetime(?)",
        "datetime(c.expires_at)>datetime(?)",
        "c.observation_ref=o.observation_ref",
        "v.project_ref=o.project_ref",
        "v.workstream_ref=o.workstream_ref",
        "v.object_ref=o.object_ref",
        "v.version_ref=o.version_ref",
        "v.subject_ref=o.subject_ref",
        "EXISTS (SELECT 1 FROM context_candidate_evidence ce0 "
        "JOIN context_evidence e0 ON e0.evidence_ref=ce0.evidence_ref "
        "WHERE ce0.candidate_ref=c.candidate_ref)",
        "NOT EXISTS (SELECT 1 FROM context_candidate_evidence ce1 "
        "JOIN context_evidence e1 ON e1.evidence_ref=ce1.evidence_ref "
        "WHERE ce1.candidate_ref=c.candidate_ref AND "
        "(e1.status<>'active' OR datetime(e1.expires_at)<=datetime(?) "
        "OR e1.provenance_ref='' OR e1.evidence_tier='P5' OR e1.state='unverified_clue'))",
    ]
    if document_dependency_only:
        where.append(
            "EXISTS (SELECT 1 FROM context_candidate_evidence cde "
            "JOIN context_evidence de ON de.evidence_ref=cde.evidence_ref "
            "JOIN context_observations dob ON dob.observation_ref=de.observation_ref "
            "JOIN context_document_source_versions dsv ON dsv.evidence_ref=de.evidence_ref "
            "JOIN context_source_events dse ON dse.source_event_ref=dsv.source_event_ref "
            "JOIN context_document_mappings dm ON dm.mapping_ref=dsv.mapping_ref "
            " AND dm.mapping_version=dsv.mapping_version AND dm.root_id=dsv.root_id "
            "JOIN context_principals dp ON dp.principal_id=de.principal_id AND dp.key_id=de.key_id "
            "WHERE cde.candidate_ref=c.candidate_ref AND dsv.status='active' "
            "AND de.status='active' AND dob.status='active' AND dse.status='active' "
            "AND dm.status='active' AND dp.status='active' "
            "AND dsv.observation_ref=de.observation_ref "
            "AND dob.source_event_ref=dsv.source_event_ref "
            "AND de.provenance_ref=dsv.document_source_ref "
            "AND de.principal_id=dm.principal_id AND de.key_id=dm.key_id "
            "AND de.adapter_kind=dp.principal_kind "
            "AND datetime(de.expires_at)>datetime(?) "
            "AND datetime(dob.expires_at)>datetime(?) "
            "AND datetime(dm.effective_at)<=datetime(?) "
            "AND (dm.expires_at='' OR datetime(dm.expires_at)>datetime(?)))"
        )
        params.extend([_iso(now), _iso(now), _iso(now), _iso(now)])
    if project_ref:
        where.append("v.project_ref=?")
        params.append(project_ref)
    if start_at:
        where.append("datetime(v.valid_from)>=datetime(?)")
        params.append(start_at)
    if end_at:
        where.append("datetime(v.valid_from)<datetime(?)")
        params.append(end_at)
    join_search = ""
    if query:
        join_search = "JOIN context_search_index f ON f.verified_ref=v.verified_ref"
        where.append("context_search_index MATCH ?")
        params.append(_fts_query(query))
    params.append(limit)
    rows = conn.execute(
        f"SELECT DISTINCT v.* FROM context_verified v "
        f"JOIN context_observations o ON o.observation_ref=v.observation_ref "
        f"JOIN context_candidates c ON c.candidate_ref=v.candidate_ref "
        f"JOIN context_label_registry lp ON lp.ref=v.project_ref AND lp.kind='project' "
        f"  AND lp.status='active' AND lp.project_ref=v.project_ref "
        f"JOIN context_label_registry lw ON lw.ref=v.workstream_ref AND lw.kind='workstream' "
        f"  AND lw.status='active' AND lw.project_ref=v.project_ref "
        f"JOIN context_label_registry lo ON lo.ref=v.object_ref AND lo.kind='object' "
        f"  AND lo.status='active' AND lo.project_ref=v.project_ref "
        f"JOIN context_label_registry lv ON lv.ref=v.version_ref AND lv.kind='version' "
        f"  AND lv.status='active' AND lv.project_ref=v.project_ref "
        f"JOIN context_label_registry ls ON ls.ref=v.subject_ref AND ls.kind='subject' "
        f"  AND ls.status='active' AND ls.project_ref=v.project_ref {join_search} "
        f"WHERE {' AND '.join(where)} ORDER BY datetime(v.valid_from) DESC,v.verified_ref LIMIT ?",
        tuple(params),
    ).fetchall()
    output: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        graph = _load_verified_graph(conn, str(row["verified_ref"]))
        if not graph:
            continue
        try:
            _validate_verified_graph(conn, graph, now)
        except ContractError:
            continue
        output.append((str(row["verified_ref"]), _public_verified_context(conn, row)))
    return output


def _select_unverified_clues(
    conn: sqlite3.Connection,
    *,
    project_ref: str,
    start_at: str,
    end_at: str,
    query: str,
    limit: int,
    now: datetime,
) -> list[dict[str, Any]]:
    params: list[Any] = [_iso(now)]
    where = [
        "o.status='active'",
        "o.trust_partition='unverified_clue'",
        "o.source_kind IN ('screen','ocr','ai_lifecycle')",
        "datetime(o.expires_at)>datetime(?)",
    ]
    if project_ref:
        where.append("o.project_ref=?")
        params.append(project_ref)
    if start_at:
        where.append("datetime(o.occurred_at)>=datetime(?)")
        params.append(start_at)
    if end_at:
        where.append("datetime(o.occurred_at)<datetime(?)")
        params.append(end_at)
    join_search = ""
    if query:
        join_search = (
            "JOIN context_search_index f "
            "ON f.verified_ref=('clue:' || o.observation_ref) "
        )
        where.append("context_search_index MATCH ?")
        params.append(_fts_query(query))
    params.append(limit)
    rows = conn.execute(
        f"SELECT o.* FROM context_observations o {join_search}"
        f"JOIN context_label_registry lp ON lp.ref=o.project_ref AND lp.kind='project' "
        f"  AND lp.status='active' AND lp.project_ref=o.project_ref "
        f"JOIN context_label_registry lw ON lw.ref=o.workstream_ref AND lw.kind='workstream' "
        f"  AND lw.status='active' AND lw.project_ref=o.project_ref "
        f"JOIN context_label_registry lo ON lo.ref=o.object_ref AND lo.kind='object' "
        f"  AND lo.status='active' AND lo.project_ref=o.project_ref "
        f"JOIN context_label_registry lv ON lv.ref=o.version_ref AND lv.kind='version' "
        f"  AND lv.status='active' AND lv.project_ref=o.project_ref "
        f"JOIN context_label_registry ls ON ls.ref=o.subject_ref AND ls.kind='subject' "
        f"  AND ls.status='active' AND ls.project_ref=o.project_ref "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY datetime(o.occurred_at) DESC,o.observation_ref LIMIT ?",
        tuple(params),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        project = _public_label(conn, str(row["project_ref"]), "project")
        output.append(
            {
                "source_ref": str(row["source_ref"]),
                "clue_type": str(row["source_kind"]),
                "observed_at": str(row["occurred_at"]),
                "expires_at": str(row["expires_at"]),
                "revocation_state": "active",
                "privacy_class": str(row["privacy_class"]),
                "project": project,
                "verification_state": "unverified",
            }
        )
    return output


def _count_valid_verified_contexts(conn: sqlite3.Connection, now: datetime) -> int:
    stamp = _iso(now)
    return _count_query(
        conn,
        "SELECT COUNT(DISTINCT v.verified_ref) FROM context_verified v "
        "JOIN context_observations o ON o.observation_ref=v.observation_ref "
        "JOIN context_candidates c ON c.candidate_ref=v.candidate_ref "
        "JOIN context_label_registry lp ON lp.ref=v.project_ref AND lp.kind='project' "
        " AND lp.status='active' AND lp.project_ref=v.project_ref "
        "JOIN context_label_registry lw ON lw.ref=v.workstream_ref AND lw.kind='workstream' "
        " AND lw.status='active' AND lw.project_ref=v.project_ref "
        "JOIN context_label_registry lo ON lo.ref=v.object_ref AND lo.kind='object' "
        " AND lo.status='active' AND lo.project_ref=v.project_ref "
        "JOIN context_label_registry lv ON lv.ref=v.version_ref AND lv.kind='version' "
        " AND lv.status='active' AND lv.project_ref=v.project_ref "
        "JOIN context_label_registry ls ON ls.ref=v.subject_ref AND ls.kind='subject' "
        " AND ls.status='active' AND ls.project_ref=v.project_ref "
        "WHERE v.status='active' AND v.provenance_ref<>'' "
        "AND o.status='active' AND o.trust_partition='verified_eligible' "
        "AND o.source_kind NOT IN ('screen','ocr') "
        "AND c.status='active' AND c.disposition='verified' AND c.observation_ref=o.observation_ref "
        "AND v.project_ref=o.project_ref AND v.workstream_ref=o.workstream_ref "
        "AND v.object_ref=o.object_ref AND v.version_ref=o.version_ref AND v.subject_ref=o.subject_ref "
        "AND datetime(v.expires_at)>datetime(?) AND datetime(o.expires_at)>datetime(?) "
        "AND datetime(c.expires_at)>datetime(?) "
        "AND EXISTS (SELECT 1 FROM context_candidate_evidence ce0 "
        " JOIN context_evidence e0 ON e0.evidence_ref=ce0.evidence_ref "
        " WHERE ce0.candidate_ref=c.candidate_ref) "
        "AND NOT EXISTS (SELECT 1 FROM context_candidate_evidence ce1 "
        " JOIN context_evidence e1 ON e1.evidence_ref=ce1.evidence_ref "
        " WHERE ce1.candidate_ref=c.candidate_ref AND "
        " (e1.status<>'active' OR datetime(e1.expires_at)<=datetime(?) OR e1.provenance_ref='' "
        " OR e1.evidence_tier='P5' OR e1.state='unverified_clue'))",
        (stamp, stamp, stamp, stamp),
    )


def _count_valid_unverified_clues(conn: sqlite3.Connection, now: datetime) -> int:
    stamp = _iso(now)
    return _count_query(
        conn,
        "SELECT COUNT(DISTINCT o.observation_ref) FROM context_observations o "
        "JOIN context_label_registry lp ON lp.ref=o.project_ref AND lp.kind='project' "
        " AND lp.status='active' AND lp.project_ref=o.project_ref "
        "JOIN context_label_registry lw ON lw.ref=o.workstream_ref AND lw.kind='workstream' "
        " AND lw.status='active' AND lw.project_ref=o.project_ref "
        "JOIN context_label_registry lo ON lo.ref=o.object_ref AND lo.kind='object' "
        " AND lo.status='active' AND lo.project_ref=o.project_ref "
        "JOIN context_label_registry lv ON lv.ref=o.version_ref AND lv.kind='version' "
        " AND lv.status='active' AND lv.project_ref=o.project_ref "
        "JOIN context_label_registry ls ON ls.ref=o.subject_ref AND ls.kind='subject' "
        " AND ls.status='active' AND ls.project_ref=o.project_ref "
        "WHERE o.status='active' AND o.trust_partition='unverified_clue' "
        "AND o.source_kind IN ('screen','ocr','ai_lifecycle') "
        "AND datetime(o.expires_at)>datetime(?)",
        (stamp,),
    )


def _count_valid_memory_candidates(conn: sqlite3.Connection, now: datetime) -> int:
    # Creation, replay, and read-side counts intentionally share the exact same
    # eligibility predicate.  Stale rows remain auditable but never surface.
    rows = conn.execute(
        "SELECT * FROM context_memory_candidates ORDER BY memory_candidate_ref"
    ).fetchall()
    total = 0
    for row in rows:
        try:
            _validate_memory_candidate_eligible(conn, row, now)
        except (ContractError, ValueError, TypeError):
            continue
        total += 1
    return total

def _tombstone_observation_source_locked(
    conn: sqlite3.Connection,
    observation_ref: str,
    *,
    reason_code: str,
    stamp: str,
) -> int:
    rows = conn.execute(
        "SELECT source_event_ref,principal_id,source_ref FROM context_observations "
        "WHERE observation_ref=? AND source_event_ref<>''",
        (observation_ref,),
    ).fetchall()
    created = 0
    for row in rows:
        source_event_ref = str(row["source_event_ref"] or "")
        principal_id = str(row["principal_id"] or "")
        source_ref = str(row["source_ref"] or "")
        if not source_event_ref:
            continue
        conn.execute(
            "UPDATE context_source_events SET status='tombstoned',tombstoned_at=?,tombstone_reason_code=? "
            "WHERE source_event_ref=? AND status='active'",
            (stamp, reason_code, source_event_ref),
        )
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO context_source_event_tombstones(source_event_ref,principal_id,source_ref,"
            "observation_ref,reason_code,created_at) VALUES(?,?,?,?,?,?)",
            (source_event_ref, principal_id, source_ref, observation_ref, reason_code, stamp),
        )
        if conn.total_changes > before:
            created += 1
    return created


def _candidate_has_valid_surviving_evidence_locked(
    conn: sqlite3.Connection,
    candidate_ref: str,
    excluded_evidence_refs: Sequence[str],
    *,
    stamp: str,
) -> bool:
    if not excluded_evidence_refs:
        return False
    placeholders = ",".join("?" for _ in excluded_evidence_refs)
    row = conn.execute(
        "SELECT 1 FROM context_candidates c "
        "JOIN context_observations co ON co.observation_ref=c.observation_ref "
        "JOIN context_candidate_evidence ce ON ce.candidate_ref=c.candidate_ref "
        "JOIN context_evidence e ON e.evidence_ref=ce.evidence_ref "
        "JOIN context_observations eo ON eo.observation_ref=e.observation_ref "
        "JOIN context_principals p ON p.principal_id=e.principal_id AND p.key_id=e.key_id "
        "JOIN context_label_registry lp ON lp.ref=co.project_ref AND lp.kind='project' "
        " AND lp.status='active' AND lp.project_ref=co.project_ref "
        "JOIN context_label_registry lw ON lw.ref=co.workstream_ref AND lw.kind='workstream' "
        " AND lw.status='active' AND lw.project_ref=co.project_ref "
        "JOIN context_label_registry lo ON lo.ref=co.object_ref AND lo.kind='object' "
        " AND lo.status='active' AND lo.project_ref=co.project_ref "
        "JOIN context_label_registry lv ON lv.ref=co.version_ref AND lv.kind='version' "
        " AND lv.status='active' AND lv.project_ref=co.project_ref "
        "JOIN context_label_registry ls ON ls.ref=co.subject_ref AND ls.kind='subject' "
        " AND ls.status='active' AND ls.project_ref=co.project_ref "
        "WHERE c.candidate_ref=? "
        f"AND ce.evidence_ref NOT IN ({placeholders}) "
        "AND c.status='active' AND co.status='active' "
        "AND e.status='active' AND eo.status='active' AND p.status='active' "
        "AND datetime(c.expires_at)>datetime(?) "
        "AND datetime(co.expires_at)>datetime(?) "
        "AND datetime(e.expires_at)>datetime(?) "
        "AND datetime(eo.expires_at)>datetime(?) "
        "AND e.provenance_ref<>'' AND e.evidence_tier<>'P5' AND e.state<>'unverified_clue' "
        "AND e.adapter_kind=p.principal_kind "
        "AND (p.project_ref='' OR p.project_ref=e.project_ref) "
        "AND e.project_ref=co.project_ref AND e.workstream_ref=co.workstream_ref "
        "AND e.object_ref=co.object_ref AND e.version_ref=co.version_ref "
        "AND e.subject_ref=co.subject_ref "
        "AND ABS((julianday(e.occurred_at)-julianday(co.occurred_at))*86400.0)<=? "
        "AND datetime(e.occurred_at)<=datetime(co.occurred_at,'+2 minutes') "
        "AND EXISTS (SELECT 1 FROM context_source_events se "
        " WHERE se.source_event_ref=eo.source_event_ref AND se.status='active') "
        "LIMIT 1",
        (
            candidate_ref,
            *excluded_evidence_refs,
            stamp,
            stamp,
            stamp,
            stamp,
            EVIDENCE_BINDING_WINDOW.total_seconds(),
        ),
    ).fetchone()
    return row is not None


def _cascade_observation_locked(
    conn: sqlite3.Connection,
    observation_ref: str,
    *,
    terminal_status: str,
    reason_code: str,
    now: datetime,
    tombstone_source: bool = True,
) -> dict[str, int]:
    """Revoke an observation and every materialized context that depends on it.

    The dependency edge is explicit: a ContextCandidate may belong to a later
    observation while citing Evidence owned by the deleted observation.  Those
    dependent candidates and their VerifiedContext projections must be revoked
    as well; otherwise a source deletion could leave trusted output behind.
    """
    stamp = _iso(now)
    cascade = _zero_cascade()
    if tombstone_source:
        cascade[
            "source_event_tombstones_created"
        ] = _tombstone_observation_source_locked(
            conn,
            observation_ref,
            reason_code=reason_code,
            stamp=stamp,
        )
    source_version_cursor = conn.execute(
        "UPDATE context_document_source_versions SET status='revoked',revoked_at=?,"
        "revocation_reason_code=? WHERE observation_ref=? AND status='active'",
        (stamp, reason_code, observation_ref),
    )
    cascade["document_source_versions_revoked"] = int(source_version_cursor.rowcount or 0)

    evidence_rows = conn.execute(
        "SELECT evidence_ref FROM context_evidence WHERE observation_ref=?",
        (observation_ref,),
    ).fetchall()
    evidence_refs = sorted({str(row["evidence_ref"]) for row in evidence_rows})

    candidate_rows = conn.execute(
        "SELECT candidate_ref FROM context_candidates WHERE observation_ref=?",
        (observation_ref,),
    ).fetchall()
    direct_candidate_refs = {str(row["candidate_ref"]) for row in candidate_rows}
    candidate_refs = set(direct_candidate_refs)
    if evidence_refs:
        evidence_placeholders = ",".join("?" for _ in evidence_refs)
        required_rows = conn.execute(
            f"SELECT DISTINCT candidate_ref FROM context_candidate_evidence "
            f"WHERE evidence_ref IN ({evidence_placeholders}) "
            "AND dependency_role IN ('primary','required')",
            tuple(evidence_refs),
        ).fetchall()
        candidate_refs.update(str(row["candidate_ref"]) for row in required_rows)
        supplemental_rows = conn.execute(
            f"SELECT candidate_ref,evidence_ref FROM context_candidate_evidence "
            f"WHERE evidence_ref IN ({evidence_placeholders}) AND dependency_role='supplemental'",
            tuple(evidence_refs),
        ).fetchall()
        for supplemental in supplemental_rows:
            candidate_ref = str(supplemental["candidate_ref"])
            if candidate_ref in candidate_refs:
                continue
            if not _candidate_has_valid_surviving_evidence_locked(
                conn,
                candidate_ref,
                evidence_refs,
                stamp=stamp,
            ):
                candidate_refs.add(candidate_ref)
                continue
            cursor = conn.execute(
                "DELETE FROM context_candidate_evidence WHERE candidate_ref=? AND evidence_ref=? "
                "AND dependency_role='supplemental'",
                (candidate_ref, str(supplemental["evidence_ref"])),
            )
            cascade["supplemental_evidence_links_removed"] += int(cursor.rowcount or 0)
    ordered_candidate_refs = sorted(candidate_refs)

    if ordered_candidate_refs:
        candidate_placeholders = ",".join("?" for _ in ordered_candidate_refs)
        verified_rows = conn.execute(
            f"SELECT verified_ref FROM context_verified WHERE observation_ref=? "
            f"OR candidate_ref IN ({candidate_placeholders})",
            (observation_ref, *ordered_candidate_refs),
        ).fetchall()
    else:
        verified_rows = conn.execute(
            "SELECT verified_ref FROM context_verified WHERE observation_ref=?",
            (observation_ref,),
        ).fetchall()
    verified_refs = sorted({str(row["verified_ref"]) for row in verified_rows})

    cursor = conn.execute(
        "UPDATE context_observations SET status=?,revoked_at=?,revocation_reason_code=? "
        "WHERE observation_ref=? AND status='active'",
        (terminal_status, stamp, reason_code, observation_ref),
    )
    cascade["observations_revoked"] = int(cursor.rowcount or 0)
    conn.execute(
        "DELETE FROM context_search_index WHERE verified_ref=?",
        (f"clue:{observation_ref}",),
    )

    cursor = conn.execute(
        "UPDATE context_evidence SET status='revoked',revoked_at=?,revocation_reason_code=? "
        "WHERE observation_ref=? AND status='active'",
        (stamp, reason_code, observation_ref),
    )
    cascade["evidence_revoked"] = int(cursor.rowcount or 0)
    cursor = conn.execute(
        "UPDATE context_activity_confirmation_links SET status='revoked',revoked_at=?,"
        "revocation_reason_code=?,updated_at=? WHERE observation_ref=? "
        "AND status='verified'",
        (stamp, reason_code, stamp, observation_ref),
    )
    cascade["confirmation_links_revoked"] = int(cursor.rowcount or 0)

    if ordered_candidate_refs:
        candidate_placeholders = ",".join("?" for _ in ordered_candidate_refs)
        cursor = conn.execute(
            f"UPDATE context_candidates SET status='revoked',revoked_at=?,revocation_reason_code=? "
            f"WHERE candidate_ref IN ({candidate_placeholders}) AND status='active'",
            (stamp, reason_code, *ordered_candidate_refs),
        )
        cascade["candidates_revoked"] = int(cursor.rowcount or 0)

    if verified_refs:
        verified_placeholders = ",".join("?" for _ in verified_refs)
        cursor = conn.execute(
            f"UPDATE context_verified SET status='revoked',revoked_at=?,revocation_reason_code=? "
            f"WHERE verified_ref IN ({verified_placeholders}) AND status='active'",
            (stamp, reason_code, *verified_refs),
        )
        cascade["verified_contexts_revoked"] = int(cursor.rowcount or 0)
        conn.execute(
            f"DELETE FROM context_search_index WHERE verified_ref IN ({verified_placeholders})",
            tuple(verified_refs),
        )
        cursor = conn.execute(
            f"UPDATE context_memory_candidates SET status='revoked',revoked_at=?,revocation_reason_code=? "
            f"WHERE verified_ref IN ({verified_placeholders}) AND status='candidate_only'",
            (stamp, reason_code, *verified_refs),
        )
        cascade["memory_candidates_revoked"] = int(cursor.rowcount or 0)
        cascade.update(
            _restrict_bundles_for_members_locked(
                conn,
                observation_ref=observation_ref,
                verified_refs=verified_refs,
                reason_code=reason_code,
                stamp=stamp,
            )
        )
    return cascade


def _revoke_activity_confirmation_links_locked(
    conn: sqlite3.Connection,
    confirmation_keys: Iterable[str],
    *,
    reason_code: str,
    now: datetime,
) -> int:
    """Revoke every live Phase 2 action bound to deleted legacy confirmations.

    Non-verifying answers have no Observation to cascade, so the durable link
    itself is their deletion receipt. Verified answers additionally revoke the
    governed graph before the link is marked terminal.
    """
    keys = sorted({str(key) for key in confirmation_keys if str(key)})
    if not keys or not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='context_activity_confirmation_links'"
    ).fetchone():
        return 0
    placeholders = ",".join("?" for _ in keys)
    links = conn.execute(
        "SELECT link_ref,observation_ref,status "
        "FROM context_activity_confirmation_links "
        f"WHERE confirmation_key IN ({placeholders}) "
        "AND status IN ('verified','non_verifying')",
        tuple(keys),
    ).fetchall()
    if not links:
        return 0
    for observation_ref in sorted(
        {
            str(link["observation_ref"])
            for link in links
            if str(link["status"]) == "verified"
            and str(link["observation_ref"])
        }
    ):
        _cascade_observation_locked(
            conn,
            observation_ref,
            reason_code=reason_code,
            terminal_status="revoked",
            now=now,
        )
    link_refs = sorted({str(link["link_ref"]) for link in links})
    link_placeholders = ",".join("?" for _ in link_refs)
    stamp = _iso(now)
    conn.execute(
        "UPDATE context_activity_confirmation_links "
        "SET status='revoked',revoked_at=?,revocation_reason_code=?,updated_at=? "
        f"WHERE link_ref IN ({link_placeholders}) "
        "AND status IN ('verified','non_verifying')",
        (stamp, reason_code, stamp, *link_refs),
    )
    return len(link_refs)


def _expire_verified_locked(conn: sqlite3.Connection, verified_ref: str, now: datetime) -> dict[str, int]:
    stamp = _iso(now)
    result = {
        "memory_candidates_revoked": 0,
        "bundle_members_revoked": 0,
        "withdrawal_receipts_created": 0,
    }
    row = conn.execute(
        "SELECT observation_ref FROM context_verified WHERE verified_ref=? AND status='active'", (verified_ref,)
    ).fetchone()
    if not row:
        return result
    conn.execute(
        "UPDATE context_verified SET status='expired',revoked_at=?,revocation_reason_code='verified_retention_expired' "
        "WHERE verified_ref=? AND status='active'",
        (stamp, verified_ref),
    )
    conn.execute("DELETE FROM context_search_index WHERE verified_ref=?", (verified_ref,))
    cursor = conn.execute(
        "UPDATE context_activity_confirmation_links SET status='expired',revoked_at=?,"
        "revocation_reason_code='verified_retention_expired',updated_at=? "
        "WHERE verified_ref=? AND status='verified'",
        (stamp, stamp, verified_ref),
    )
    result["confirmation_links_revoked"] = int(cursor.rowcount or 0)
    cursor = conn.execute(
        "UPDATE context_memory_candidates SET status='revoked',revoked_at=?,"
        "revocation_reason_code='verified_retention_expired' WHERE verified_ref=? AND status='candidate_only'",
        (stamp, verified_ref),
    )
    result["memory_candidates_revoked"] = int(cursor.rowcount or 0)
    result.update(
        _restrict_bundles_for_members_locked(
            conn,
            observation_ref=str(row["observation_ref"]),
            verified_refs=[verified_ref],
            reason_code="verified_retention_expired",
            stamp=stamp,
        )
    )
    return result


def _restrict_bundles_for_members_locked(
    conn: sqlite3.Connection,
    *,
    observation_ref: str,
    verified_refs: Sequence[str],
    reason_code: str,
    stamp: str,
) -> dict[str, int]:
    result = {
        "bundle_members_revoked": 0,
        "withdrawal_receipts_created": 0,
        "delivery_failures_recorded": 0,
    }
    if not verified_refs:
        return result
    placeholders = ",".join("?" for _ in verified_refs)
    bundle_rows = conn.execute(
        f"SELECT DISTINCT bundle_ref FROM context_bundle_members WHERE member_ref IN ({placeholders})",
        tuple(verified_refs),
    ).fetchall()
    bundle_refs = sorted({str(row["bundle_ref"]) for row in bundle_rows})
    cursor = conn.execute(
        f"UPDATE context_bundle_members SET status='revoked',revoked_at=?,revocation_reason_code=? "
        f"WHERE member_ref IN ({placeholders}) AND status='active'",
        (stamp, reason_code, *verified_refs),
    )
    result["bundle_members_revoked"] = int(cursor.rowcount or 0)
    for bundle_ref in bundle_refs:
        conn.execute(
            "UPDATE context_bundle_manifests SET status='restricted',restricted_at=?,restriction_reason_code=? "
            "WHERE bundle_ref=? AND status='active'",
            (stamp, reason_code, bundle_ref),
        )
        delivery_rows = conn.execute(
            "SELECT delivery_ref,bundle_ref,bundle_hash,state FROM context_deliveries WHERE bundle_ref=?",
            (bundle_ref,),
        ).fetchall()
        dependency = _restrict_deliveries_for_dependency_locked(
            conn,
            delivery_rows=delivery_rows,
            reason_code=reason_code,
            stamp=stamp,
        )
        _merge_cascade(result, dependency)
    return result


def _restrict_deliveries_for_dependency_locked(
    conn: sqlite3.Connection,
    *,
    delivery_rows: Sequence[sqlite3.Row | Mapping[str, Any]],
    reason_code: str,
    stamp: str,
) -> dict[str, int]:
    result = {
        "withdrawal_receipts_created": 0,
        "delivery_failures_recorded": 0,
    }
    for supplied in delivery_rows:
        delivery_ref = str(supplied["delivery_ref"])
        delivery = conn.execute(
            "SELECT * FROM context_deliveries WHERE delivery_ref=?", (delivery_ref,)
        ).fetchone()
        if not delivery:
            continue
        bundle_ref = str(delivery["bundle_ref"])
        bundle_hash = str(delivery["bundle_hash"])
        state = str(delivery["state"] or "")
        if state == "delivered":
            observations = conn.execute(
                "SELECT DISTINCT v.observation_ref FROM context_bundle_members bm "
                "JOIN context_verified v ON v.verified_ref=bm.member_ref "
                "WHERE bm.bundle_ref=? AND bm.member_kind='verified_context'",
                (bundle_ref,),
            ).fetchall()
            if not observations:
                observations = [{"observation_ref": "observation_dependencyrestriction"}]
            for observation in observations:
                observation_ref = str(observation["observation_ref"] or "observation_dependencyrestriction")
                withdrawal_ref = _derived_ref(
                    "withdrawal", f"{reason_code}|{observation_ref}|{delivery_ref}"
                )
                before = conn.total_changes
                conn.execute(
                    "INSERT OR IGNORE INTO context_withdrawal_receipts(withdrawal_ref,observation_ref,bundle_ref,"
                    "delivery_ref,reason_code,status,created_at) VALUES(?,?,?,?,?,'local_restriction_recorded',?)",
                    (withdrawal_ref, observation_ref, bundle_ref, delivery_ref, reason_code, stamp),
                )
                if conn.total_changes > before:
                    result["withdrawal_receipts_created"] += 1
        elif state in {"prepared", "consumed"}:
            cursor = conn.execute(
                "UPDATE context_deliveries SET state='failed',failed_at=?,failure_reason_code='source_revoked',updated_at=? "
                "WHERE delivery_ref=? AND state IN ('prepared','consumed')",
                (stamp, stamp, delivery_ref),
            )
            if int(cursor.rowcount or 0):
                receipt_ref = _derived_ref("receipt", f"dependency|{reason_code}|{delivery_ref}")
                idempotency_key = _derived_ref("idem", f"dependency|{reason_code}|{delivery_ref}")
                before = conn.total_changes
                conn.execute(
                    "INSERT OR IGNORE INTO context_delivery_receipts(receipt_ref,delivery_ref,principal_id,key_id,"
                    "idempotency_key,payload_hash,bundle_ref,attempt_no,project_ref,target_ref,target_scope,"
                    "export_policy,privacy_policy_hash,principal_role,transition,occurred_at,bundle_hash,"
                    "provider_receipt_ref,failure_reason_code,result_state,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'failed',?,?, '', 'source_revoked','failed',?)",
                    (
                        receipt_ref,
                        delivery_ref,
                        "principal_localcore",
                        "key_localcore",
                        idempotency_key,
                        _hash_text(
                            f"{reason_code}|{delivery_ref}|{bundle_hash}|{delivery['privacy_policy_hash']}"
                        ),
                        bundle_ref,
                        int(delivery["attempt_no"] or 0),
                        str(delivery["project_ref"] or ""),
                        str(delivery["target_ref"] or ""),
                        str(delivery["target_scope"] or ""),
                        str(delivery["export_policy"] or ""),
                        str(delivery["privacy_policy_hash"] or ""),
                        "system",
                        stamp,
                        bundle_hash,
                        stamp,
                    ),
                )
                if conn.total_changes > before:
                    result["delivery_failures_recorded"] += 1
    return result

def _index_verified(conn: sqlite3.Connection, verified_ref: str) -> None:
    row = conn.execute("SELECT * FROM context_verified WHERE verified_ref=?", (verified_ref,)).fetchone()
    if not row:
        return
    labels = _labels_for_context(conn, row)
    conn.execute("DELETE FROM context_search_index WHERE verified_ref=?", (verified_ref,))
    conn.execute(
        "INSERT INTO context_search_index(verified_ref,project_ref,occurred_at,project_label,workstream_label,object_label,"
        "version_label,state) VALUES(?,?,?,?,?,?,?,?)",
        (
            verified_ref,
            str(row["project_ref"]),
            str(row["valid_from"]),
            labels["project"]["label"],
            labels["workstream"]["label"],
            labels["object"]["label"],
            labels["version"]["label"],
            str(row["state"]),
        ),
    )


def _index_unverified_clue(conn: sqlite3.Connection, observation_ref: str) -> None:
    row = conn.execute(
        "SELECT * FROM context_observations WHERE observation_ref=? "
        "AND status='active' AND trust_partition='unverified_clue'",
        (observation_ref,),
    ).fetchone()
    if not row:
        return
    labels = _labels_for_context(conn, row)
    clue_index_ref = f"clue:{observation_ref}"
    conn.execute(
        "DELETE FROM context_search_index WHERE verified_ref=?",
        (clue_index_ref,),
    )
    conn.execute(
        "INSERT INTO context_search_index(verified_ref,project_ref,occurred_at,project_label,workstream_label,"
        "object_label,version_label,state) VALUES(?,?,?,?,?,?,?,'unverified_clue')",
        (
            clue_index_ref,
            str(row["project_ref"]),
            str(row["occurred_at"]),
            labels["project"]["label"],
            labels["workstream"]["label"],
            labels["object"]["label"],
            labels["version"]["label"],
        ),
    )


def _labels_for_context(conn: sqlite3.Connection, row: sqlite3.Row | Mapping[str, Any]) -> dict[str, dict[str, str]]:
    return {
        "project": _public_label(conn, str(row["project_ref"]), "project"),
        "workstream": _public_label(conn, str(row["workstream_ref"]), "workstream"),
        "object": _public_label(conn, str(row["object_ref"]), "object"),
        "version": _public_label(conn, str(row["version_ref"]), "version"),
        "subject": _public_label(conn, str(row["subject_ref"]), "subject"),
    }


def _public_verified_context(
    conn: sqlite3.Connection,
    row: sqlite3.Row | Mapping[str, Any],
) -> dict[str, Any]:
    labels = _labels_for_context(conn, row)
    public_projection = {
        "source_ref": str(row["source_ref"]),
        "state": str(row["state"]),
        "occurred_at": str(row["valid_from"]),
        "expires_at": str(row["expires_at"]),
        "revocation_state": "active",
        "privacy_class": str(row["privacy_class"]),
        "project": labels["project"],
        "workstream": labels["workstream"],
        "object": labels["object"],
        "version": labels["version"],
        "subject": labels["subject"],
        "source": {
            "kind": str(row["source_kind"]),
            "evidence_tier": str(row["evidence_tier"]),
        },
    }
    context_ref = _derived_ref(
        "context",
        json.dumps(public_projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
    )
    return {"context_ref": context_ref, **public_projection}


def _load_verified_graph(conn: sqlite3.Connection, verified_ref: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT v.*,"
        "o.status AS observation_status,o.trust_partition,o.expires_at AS observation_expires_at,"
        "o.project_ref AS observation_project_ref,o.workstream_ref AS observation_workstream_ref,"
        "o.object_ref AS observation_object_ref,o.version_ref AS observation_version_ref,"
        "o.subject_ref AS observation_subject_ref,o.source_kind AS observation_source_kind,"
        "c.status AS candidate_status,c.disposition,c.expires_at AS candidate_expires_at,"
        "c.observation_ref AS candidate_observation_ref "
        "FROM context_verified v "
        "JOIN context_observations o ON o.observation_ref=v.observation_ref "
        "JOIN context_candidates c ON c.candidate_ref=v.candidate_ref "
        "WHERE v.verified_ref=?",
        (verified_ref,),
    ).fetchone()


def _validate_verified_graph(
    conn: sqlite3.Connection,
    row: sqlite3.Row | Mapping[str, Any],
    now: datetime,
) -> None:
    if str(row["status"] or "") != "active" or str(row["observation_status"] or "") != "active":
        raise ContractError("verified_context_revoked")
    if str(row["candidate_status"] or "") != "active" or str(row["disposition"] or "") != "verified":
        raise ContractError("candidate_not_verified")
    if str(row["trust_partition"] or "") != "verified_eligible":
        raise ContractError("unverified_clue_forbidden")
    if str(row["source_kind"] or "") in {"screen", "ocr"}:
        raise ContractError("unverified_clue_forbidden")
    if str(row["observation_source_kind"] or "") in {"screen", "ocr"}:
        raise ContractError("unverified_clue_forbidden")
    if str(row["candidate_observation_ref"] or "") != str(row["observation_ref"] or ""):
        raise ContractError("context_graph_mismatch")
    for field in ("project_ref", "workstream_ref", "object_ref", "version_ref", "subject_ref"):
        if str(row[field] or "") != str(row[f"observation_{field}"] or ""):
            raise ContractError("context_graph_mismatch")
    if _parse_contract_time(str(row["expires_at"])) <= now:
        raise ContractError("verified_context_expired")
    if _parse_contract_time(str(row["observation_expires_at"])) <= now:
        raise ContractError("verified_context_expired")
    if _parse_contract_time(str(row["candidate_expires_at"])) <= now:
        raise ContractError("candidate_expired")
    if not str(row["provenance_ref"] or ""):
        raise ContractError("provenance_missing")
    _validate_registry_refs(
        conn,
        {
            "project_ref": str(row["project_ref"]),
            "workstream_ref": str(row["workstream_ref"]),
            "object_ref": str(row["object_ref"]),
            "version_ref": str(row["version_ref"]),
            "subject_ref": str(row["subject_ref"]),
        },
    )
    evidence_rows = conn.execute(
        "SELECT e.status,e.expires_at,e.provenance_ref,e.evidence_tier,e.state,"
        "e.principal_id,e.key_id,e.adapter_kind "
        "FROM context_candidate_evidence ce "
        "JOIN context_evidence e ON e.evidence_ref=ce.evidence_ref "
        "WHERE ce.candidate_ref=? ORDER BY e.evidence_ref",
        (str(row["candidate_ref"]),),
    ).fetchall()
    if not evidence_rows:
        raise ContractError("provenance_missing")
    for evidence in evidence_rows:
        if str(evidence["status"] or "") != "active":
            raise ContractError("provenance_revoked")
        if _parse_contract_time(str(evidence["expires_at"])) <= now:
            raise ContractError("provenance_expired")
        if not str(evidence["provenance_ref"] or ""):
            raise ContractError("provenance_missing")
        if str(evidence["evidence_tier"] or "") == "P5" or str(evidence["state"] or "") == "unverified_clue":
            raise ContractError("unverified_clue_forbidden")
        principal_id = str(evidence["principal_id"] or "")
        key_id = str(evidence["key_id"] or "")
        if principal_id or key_id:
            principal = conn.execute(
                "SELECT status,principal_kind FROM context_principals WHERE principal_id=? AND key_id=?",
                (principal_id, key_id),
            ).fetchone()
            if not principal or str(principal["status"] or "") != "active":
                raise ContractError("provenance_principal_revoked")
            adapter_kind = str(evidence["adapter_kind"] or "")
            if adapter_kind and adapter_kind != str(principal["principal_kind"] or ""):
                raise ContractError("provenance_principal_mismatch")


def _public_label(conn: sqlite3.Connection, ref: str, kind: str) -> dict[str, str]:
    row = conn.execute(
        "SELECT label,subject_scope FROM context_label_registry WHERE ref=? AND kind=? AND status='active'",
        (ref, kind),
    ).fetchone()
    if not row:
        return {"ref": ref, "label": "", **({"scope": ""} if kind == "subject" else {})}
    output = {"ref": ref, "label": str(row["label"])}
    if kind == "subject":
        output["scope"] = str(row["subject_scope"] or "")
    return output


def _validate_registry_refs(conn: sqlite3.Connection, refs: Mapping[str, Any]) -> None:
    project_ref = str(refs.get("project_ref") or "")
    _require_active_label(conn, project_ref, "project")
    for field, kind in (
        ("workstream_ref", "workstream"),
        ("object_ref", "object"),
        ("version_ref", "version"),
        ("subject_ref", "subject"),
    ):
        ref = str(refs.get(field) or "")
        row = _require_active_label(conn, ref, kind)
        if str(row["project_ref"] or "") != project_ref:
            raise ContractError(f"{kind}_project_mismatch")


def _require_active_label(conn: sqlite3.Connection, ref: str, kind: str) -> sqlite3.Row:
    _validate_ref_for_kind(kind, ref)
    row = _label_row(conn, ref, kind)
    if not row:
        if kind == "project":
            raise ContractError("project_unregistered")
        raise ContractError(f"{kind}_unregistered")
    return row


def _label_row(conn: sqlite3.Connection, ref: str, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM context_label_registry WHERE ref=? AND kind=? AND status='active'",
        (ref, kind),
    ).fetchone()


def _authenticate_principal(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    key_id: str,
    verification_secret: bytes | str,
    expected_kinds: set[str],
    project_ref: str,
) -> Principal:
    row = conn.execute(
        "SELECT * FROM context_principals WHERE principal_id=? AND key_id=?",
        (principal_id, key_id),
    ).fetchone()
    if not row:
        raise ContractError("principal_unregistered")
    if str(row["status"] or "") != "active":
        raise ContractError("key_revoked")
    principal_kind = str(row["principal_kind"] or "")
    if principal_kind not in expected_kinds:
        raise ContractError("principal_kind_not_authorized")
    registered_project = str(row["project_ref"] or "")
    if project_ref and registered_project and registered_project != project_ref:
        raise ContractError("principal_project_mismatch")
    secret = _secret_bytes(verification_secret)
    if not hmac.compare_digest(str(row["key_fingerprint"]), _secret_fingerprint(secret)):
        raise ContractError("authentication_key_mismatch")
    return Principal(
        principal_id,
        key_id,
        principal_kind,
        registered_project,
        str(row["target_ref"] or ""),
        str(row["target_scope"] or ""),
        str(row["key_fingerprint"]),
    )


def _reject_document_mapping_principal_locked(
    conn: sqlite3.Connection,
    principal_id: str,
    key_id: str,
) -> None:
    """Keep mapping identities internal even for pre-fix rows typed as file_adapter."""
    mapping = conn.execute(
        "SELECT 1 FROM context_document_mappings WHERE principal_id=? AND key_id=? LIMIT 1",
        (principal_id, key_id),
    ).fetchone()
    if mapping:
        raise ContractError("document_mapping_principal_internal_only")


def _verify_signature(payload: Mapping[str, Any], secret: bytes | str) -> None:
    provided = str(payload.get("signature") or "")
    expected = sign_contract_payload(payload, secret)
    if not hmac.compare_digest(provided, expected):
        raise ContractError("signature_invalid")


def _validate_evidence_refs(
    conn: sqlite3.Connection,
    refs: Sequence[str],
    project_ref: str,
    now: datetime,
    *,
    workstream_ref: str = "",
    object_ref: str = "",
    version_ref: str = "",
    subject_ref: str = "",
    privacy_class: str = "",
    occurred_at: datetime | None = None,
) -> None:
    adapters: set[str] = set()
    principals: set[str] = set()
    fingerprints: set[str] = set()
    sources: set[str] = set()
    source_events: set[str] = set()
    provenances: set[str] = set()
    observed = occurred_at or now
    for ref in refs:
        row = conn.execute(
            "SELECT e.status,e.expires_at,e.provenance_ref,e.occurred_at,e.project_ref,e.workstream_ref,"
            "e.object_ref,e.version_ref,e.subject_ref,e.adapter_kind,e.principal_id,e.key_id,e.source_ref,"
            "e.privacy_class,o.source_event_ref,o.status AS observation_status FROM context_evidence e "
            "JOIN context_observations o ON o.observation_ref=e.observation_ref WHERE e.evidence_ref=?",
            (ref,),
        ).fetchone()
        if not row:
            raise ContractError("evidence_ref_unregistered")
        if str(row["status"] or "") != "active" or str(row["observation_status"] or "") != "active":
            raise ContractError("evidence_ref_revoked")
        provenance = str(row["provenance_ref"] or "")
        if not provenance:
            raise ContractError("evidence_provenance_missing")
        binding = (
            ("project_ref", project_ref, "evidence_project_mismatch"),
            ("workstream_ref", workstream_ref, "evidence_workstream_mismatch"),
            ("object_ref", object_ref, "evidence_object_mismatch"),
            ("version_ref", version_ref, "evidence_version_mismatch"),
            ("subject_ref", subject_ref, "evidence_subject_mismatch"),
            ("privacy_class", privacy_class, "evidence_privacy_mismatch"),
        )
        for field, expected, reason in binding:
            if expected and str(row[field] or "") != expected:
                raise ContractError(reason)
        if _parse_contract_time(str(row["expires_at"])) <= now:
            raise ContractError("evidence_ref_expired")
        evidence_time = _parse_contract_time(str(row["occurred_at"]))
        if abs((observed - evidence_time).total_seconds()) > EVIDENCE_BINDING_WINDOW.total_seconds():
            raise ContractError("evidence_time_window_mismatch")
        if evidence_time > observed + RECEIPT_FUTURE_SKEW:
            raise ContractError("evidence_time_regression")
        adapter = str(row["adapter_kind"] or "")
        principal_id = str(row["principal_id"] or "")
        key_id = str(row["key_id"] or "")
        if not principal_id or not key_id:
            raise ContractError("evidence_principal_missing")
        principal = conn.execute(
            "SELECT status,principal_kind,project_ref,key_fingerprint FROM context_principals "
            "WHERE principal_id=? AND key_id=?",
            (principal_id, key_id),
        ).fetchone()
        if not principal or str(principal["status"] or "") != "active":
            raise ContractError("evidence_principal_revoked")
        if adapter != str(principal["principal_kind"] or ""):
            raise ContractError("evidence_principal_mismatch")
        registered_project = str(principal["project_ref"] or "")
        if registered_project and registered_project != project_ref:
            raise ContractError("evidence_principal_project_mismatch")
        fingerprint = str(principal["key_fingerprint"] or "")
        source = str(row["source_ref"] or "")
        source_event = str(row["source_event_ref"] or "")
        if refs and len(refs) > 1:
            if not adapter or adapter in adapters:
                raise ContractError("independent_evidence_adapter_required")
            if principal_id in principals:
                raise ContractError("independent_evidence_principal_required")
            if not fingerprint or fingerprint in fingerprints:
                raise ContractError("independent_evidence_key_fingerprint_required")
            if not source or source in sources:
                raise ContractError("independent_evidence_source_required")
            if not source_event or source_event in source_events:
                raise ContractError("independent_evidence_source_event_required")
            if provenance in provenances:
                raise ContractError("independent_evidence_provenance_required")
        adapters.add(adapter)
        principals.add(principal_id)
        fingerprints.add(fingerprint)
        sources.add(source)
        source_events.add(source_event)
        provenances.add(provenance)


def _idempotency_decision(
    conn: sqlite3.Connection,
    *,
    table: str,
    idempotency_key: str,
    payload_hash: str,
    contract_type: str,
) -> str:
    row = conn.execute(
        f"SELECT payload_hash,contract_type FROM {table} WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if not row:
        return "new"
    if str(row["payload_hash"]) == payload_hash and str(row["contract_type"]) == contract_type:
        return "same"
    return "conflict"


def _recheck_idempotency_after_race(
    conn: sqlite3.Connection,
    table: str,
    idempotency_key: str,
    payload_hash: str,
) -> dict[str, Any]:
    try:
        row = conn.execute(
            f"SELECT payload_hash,receipt_ref,result_status FROM {table} WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
    except sqlite3.Error:
        return _result("failed", "database_write_failed")
    if row and str(row["payload_hash"]) == payload_hash:
        return {
            "status": "replayed",
            "reason_code": "idempotent_replay",
            "receipt_ref": str(row["receipt_ref"] or ""),
            "result_status": str(row["result_status"] or ""),
            "raw_access": False,
        }
    return _result("conflict", "idempotency_conflict")


def _validate_delivery_transition(current: str, transition: str) -> None:
    legal = {
        "prepared": {"consumed", "failed"},
        "consumed": {"delivered", "failed"},
        "delivered": set(),
        "failed": set(),
    }
    if transition not in legal.get(current, set()):
        raise ContractError("illegal_delivery_transition")


def _authorize_principal_for_project(principal: Principal, project_ref: str) -> None:
    if principal.project_ref and principal.project_ref != project_ref:
        raise ContractError("principal_project_mismatch")


def _validate_bundle_manifest_live(
    conn: sqlite3.Connection,
    manifest: sqlite3.Row | Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    bundle_ref = str(manifest["bundle_ref"] or "")
    bundle_hash = str(manifest["bundle_hash"] or "")
    if str(manifest["status"] or "") != "active":
        raise ContractError("bundle_restricted")
    if not _SHA256_RE.fullmatch(bundle_hash):
        raise ContractError("bundle_hash_mismatch")
    project_ref = str(manifest["project_ref"] or "")
    _require_active_label(conn, project_ref, "project")
    try:
        stable_projection = json.loads(str(manifest["stable_projection_json"] or "{}"))
        frozen_payload = json.loads(str(manifest["frozen_payload_json"] or "{}"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContractError("bundle_manifest_invalid") from exc
    if not isinstance(stable_projection, dict) or not isinstance(frozen_payload, dict):
        raise ContractError("bundle_manifest_invalid")
    _validate_public_bundle_projection(stable_projection)
    _validate_public_bundle_projection(frozen_payload)
    if stable_projection != frozen_payload:
        raise ContractError("bundle_projection_mismatch")
    if _public_hash(stable_projection) != bundle_hash or _public_hash(frozen_payload) != bundle_hash:
        raise ContractError("bundle_hash_mismatch")
    if frozen_payload.get("contract_version") != CONTEXT_BUNDLE_VERSION or frozen_payload.get("raw_access") is not False:
        raise ContractError("bundle_manifest_invalid")
    frozen_items = frozen_payload.get("verified_contexts")
    frozen_clues = frozen_payload.get("unverified_clues")
    if not isinstance(frozen_items, list) or not isinstance(frozen_clues, list):
        raise ContractError("bundle_manifest_invalid")
    verified_sources = {
        str(item.get("source_ref") or "") for item in frozen_items if isinstance(item, dict)
    }
    clue_sources = {
        str(item.get("source_ref") or "") for item in frozen_clues if isinstance(item, dict)
    }
    if verified_sources.intersection(clue_sources):
        raise ContractError("bundle_partition_overlap")

    members = conn.execute(
        "SELECT member_kind,member_ref,member_hash,status FROM context_bundle_members "
        "WHERE bundle_ref=? ORDER BY member_kind,member_ref",
        (bundle_ref,),
    ).fetchall()
    expected_count = int(manifest["item_count"] or 0)
    if len(members) != expected_count or len(frozen_items) != expected_count:
        raise ContractError("bundle_source_invalid")
    frozen_by_ref: dict[str, dict[str, Any]] = {}
    for item in frozen_items:
        if not isinstance(item, dict):
            raise ContractError("bundle_source_invalid")
        context_ref = str(item.get("context_ref") or "")
        if not context_ref or context_ref in frozen_by_ref:
            raise ContractError("bundle_source_invalid")
        frozen_by_ref[context_ref] = item
    for member in members:
        if str(member["member_kind"] or "") != "verified_context" or str(member["status"] or "") != "active":
            raise ContractError("bundle_restricted")
        member_ref = str(member["member_ref"] or "")
        try:
            _validate_internal_ref("verified", member_ref)
        except ContractError as exc:
            raise ContractError("bundle_source_invalid") from exc
        graph = _load_verified_graph(conn, member_ref)
        if not graph:
            raise ContractError("bundle_source_invalid")
        try:
            _validate_verified_graph(conn, graph, now)
        except ContractError as exc:
            raise ContractError("bundle_source_invalid") from exc
        public_item = _public_verified_context(conn, graph)
        public_ref = str(public_item["context_ref"])
        frozen_item = frozen_by_ref.get(public_ref)
        if frozen_item is None:
            raise ContractError("bundle_source_invalid")
        expected_member_hash = _public_hash(frozen_item)
        if not hmac.compare_digest(str(member["member_hash"] or ""), expected_member_hash):
            raise ContractError("bundle_member_hash_mismatch")
        if not hmac.compare_digest(_public_hash(public_item), expected_member_hash):
            raise ContractError("bundle_source_changed")
    return frozen_payload


def _validate_delivery_bundle_live(
    conn: sqlite3.Connection,
    delivery: sqlite3.Row | Mapping[str, Any],
    now: datetime,
) -> str:
    bundle_ref = str(delivery["bundle_ref"] or "")
    bundle_hash = str(delivery["bundle_hash"] or "")
    manifest = conn.execute(
        "SELECT * FROM context_bundle_manifests WHERE bundle_ref=?",
        (bundle_ref,),
    ).fetchone()
    if not manifest:
        raise ContractError("bundle_manifest_missing")
    if str(manifest["bundle_hash"] or "") != bundle_hash:
        raise ContractError("bundle_hash_mismatch")
    project_ref = str(manifest["project_ref"] or "")
    if not str(delivery["project_ref"] or "") or str(delivery["project_ref"] or "") != project_ref:
        raise ContractError("delivery_project_mismatch")
    target_ref = str(delivery["target_ref"] or "")
    target = _require_active_label(conn, target_ref, "target")
    if str(target["project_ref"] or "") != project_ref:
        raise ContractError("target_project_mismatch")
    policy = _load_target_policy(conn, target_ref, project_ref=project_ref)
    if str(policy["target_scope"]) != str(delivery["target_scope"] or ""):
        raise ContractError("target_policy_changed")
    if str(policy["export_policy"]) != str(delivery["export_policy"] or ""):
        raise ContractError("target_policy_changed")
    if str(policy["allowed_privacy_classes_json"]) != str(delivery["allowed_privacy_classes_json"] or ""):
        raise ContractError("target_policy_changed")
    if not hmac.compare_digest(
        str(policy["privacy_policy_hash"]), str(delivery["privacy_policy_hash"] or "")
    ):
        raise ContractError("target_policy_changed")
    frozen_payload = _validate_bundle_manifest_live(conn, manifest, now)
    if frozen_payload.get("unverified_clues") != []:
        raise ContractError("trusted_delivery_clues_forbidden")
    allowed_privacy = set(policy["allowed_privacy_classes"])
    for item in frozen_payload.get("verified_contexts") or []:
        if not isinstance(item, dict):
            raise ContractError("bundle_source_invalid")
        privacy = str(item.get("privacy_class") or "")
        if privacy not in allowed_privacy:
            raise ContractError("target_privacy_policy_forbidden")
        if str(delivery["target_scope"] or "") == "external" and privacy == "restricted_local":
            raise ContractError("restricted_local_export_forbidden")
    return project_ref

def _parse_closed_json(
    raw: bytes | str,
    expected_fields: set[str],
    *,
    required_fields: set[str] | None = None,
) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw_bytes = raw.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ContractError("invalid_utf8") from exc
    elif isinstance(raw, (bytes, bytearray)):
        raw_bytes = bytes(raw)
    else:
        raise ContractError("contract_body_must_be_bytes_or_text")
    if len(raw_bytes) > MAX_CONTRACT_BYTES:
        raise ContractError("contract_too_large")
    if b"\x00" in raw_bytes:
        raise ContractError("nul_forbidden")
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractError("invalid_utf8") from exc
    if _contains_unicode_control(text):
        raise ContractError("unicode_control_forbidden")

    def reject_constant(_: str) -> None:
        raise ContractError("non_finite_number_forbidden")

    def reject_large_integer(value: str) -> int:
        digits = value[1:] if value.startswith("-") else value
        if len(digits) > MAX_JSON_INTEGER_DIGITS:
            raise ContractError("integer_too_large")
        try:
            return int(value)
        except ValueError as exc:
            raise ContractError("integer_too_large") from exc

    def pairs_hook(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        normalized_keys: set[str] = set()
        for key, value in pairs:
            if not isinstance(key, str):
                raise ContractError("invalid_json_key")
            if not key.isascii():
                raise ContractError("unicode_confusable_key")
            normalized_key = unicodedata.normalize("NFKC", key)
            if key in output or normalized_key in normalized_keys:
                raise ContractError("duplicate_json_key")
            normalized_keys.add(normalized_key)
            output[key] = value
        return output

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
            parse_int=reject_large_integer,
        )
    except ContractError:
        raise
    except (json.JSONDecodeError, ValueError, OverflowError, RecursionError) as exc:
        raise ContractError("invalid_json") from exc
    if not isinstance(parsed, dict):
        raise ContractError("contract_must_be_object")
    _validate_json_shape(parsed)
    keys = set(parsed)
    missing = (expected_fields if required_fields is None else required_fields) - keys
    unknown = keys - expected_fields
    if missing:
        raise ContractError("missing_required_field")
    if unknown:
        raise ContractError("unknown_field")
    return parsed


def _validate_json_shape(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ContractError("json_too_deep")
    if isinstance(value, dict):
        if len(value) > MAX_JSON_ITEMS:
            raise ContractError("too_many_json_items")
        for item in value.values():
            _validate_json_shape(item, depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_JSON_ITEMS:
            raise ContractError("too_many_json_items")
        for item in value:
            _validate_json_shape(item, depth + 1)
        return
    if isinstance(value, str):
        if _contains_unicode_control(value):
            raise ContractError("unicode_control_forbidden")
        if len(value) > 256:
            raise ContractError("string_too_long")
        if _PATH_OR_URL_RE.search(value):
            raise ContractError("path_or_url_content_forbidden")
        if _SECRET_LIKE_RE.search(value):
            raise ContractError("secret_like_content_forbidden")
        return
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
            raise ContractError("non_finite_number_forbidden")
        return
    raise ContractError("invalid_json_value")


def _normalize_for_signature(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "occurred_at" and isinstance(value, str):
            normalized[key] = _iso(_parse_contract_time(value))
        elif key == "evidence_refs" and isinstance(value, list):
            normalized[key] = sorted(value)
        else:
            normalized[key] = value
    return normalized


def _validate_opaque_ref(field: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ContractError(f"invalid_{field}")
    prefix = _PREFIXES.get(field)
    if prefix is None:
        raise ContractError("unknown_ref_field")
    if not value.startswith(prefix):
        raise ContractError(f"invalid_{field}")
    suffix = value[len(prefix) :]
    if not _OPAQUE_SUFFIX_RE.fullmatch(suffix):
        raise ContractError(f"invalid_{field}")
    return value


def _validate_internal_ref(kind: str, value: Any) -> str:
    if not isinstance(value, str) or not value.startswith(f"{kind}_"):
        raise ContractError(f"invalid_{kind}_ref")
    if not _OPAQUE_SUFFIX_RE.fullmatch(value[len(kind) + 1 :]):
        raise ContractError(f"invalid_{kind}_ref")
    return value


def _validate_ref_for_kind(kind: str, ref: Any) -> str:
    field = f"{kind}_ref"
    if field not in _PREFIXES:
        raise ContractError("invalid_label_kind")
    return _validate_opaque_ref(field, ref)


def _validate_approved_label(value: Any) -> str:
    if not isinstance(value, str):
        raise ContractError("invalid_approved_label")
    label = unicodedata.normalize("NFKC", value).strip()
    if not label or len(label) > MAX_LABEL_LENGTH:
        raise ContractError("invalid_approved_label")
    if _contains_unicode_control(label) or _PATH_OR_URL_RE.search(label) or _SECRET_LIKE_RE.search(label):
        raise ContractError("approved_label_contains_prohibited_content")
    if "\n" in label or "\r" in label or "{" in label or "}" in label:
        raise ContractError("invalid_approved_label")
    return label


def _validate_manual_note_label(value: Any) -> str:
    """Validate the owner-authored note without weakening the public firewall."""
    if not isinstance(value, str):
        raise ContractError("invalid_manual_note")
    label = unicodedata.normalize("NFKC", value).strip()
    if not label or len(label) > 180:
        raise ContractError("invalid_manual_note")
    if (
        _contains_unicode_control(label)
        or _PATH_OR_URL_RE.search(label)
        or _SECRET_LIKE_RE.search(label)
    ):
        raise ContractError("manual_note_contains_prohibited_content")
    if "\n" in label or "\r" in label or "{" in label or "}" in label:
        raise ContractError("invalid_manual_note")
    return label


def _validate_search_query(value: Any) -> str:
    if value in {None, ""}:
        return ""
    if not isinstance(value, str):
        raise ContractError("invalid_search_query")
    query = unicodedata.normalize("NFKC", value).strip()
    if not query or len(query) > MAX_QUERY_LENGTH:
        raise ContractError("invalid_search_query")
    if _contains_unicode_control(query) or _PATH_OR_URL_RE.search(query) or _SECRET_LIKE_RE.search(query):
        raise ContractError("prohibited_search_query")
    return query


def _fts_query(query: str) -> str:
    terms = [term for term in re.split(r"\s+", query) if term]
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms[:8])


def _validate_reason_code(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", value):
        raise ContractError("invalid_reason_code")


def _parse_contract_time(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ContractError("invalid_occurred_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("invalid_occurred_at") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("timezone_required")
    return parsed.astimezone(timezone.utc)


def _parse_optional_time(value: str) -> datetime | None:
    if not value:
        return None
    return _parse_contract_time(value)


def _validate_freshness(occurred: datetime, now: datetime) -> None:
    if occurred < now - RECEIPT_MAX_AGE:
        raise ContractError("receipt_too_old")
    if occurred > now + RECEIPT_FUTURE_SKEW:
        raise ContractError("receipt_from_future")


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _payload_hash(payload: Mapping[str, Any]) -> str:
    body = dict(payload)
    body.pop("signature", None)
    return hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _public_hash(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    ).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _derived_ref(kind: str, material: str) -> str:
    prefix = "idem" if kind == "idem" else kind
    return f"{prefix}_{_hash_text(material)[:32]}"


def _secret_bytes(value: bytes | str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise ContractError("verification_secret_missing")


def _secret_fingerprint(secret: bytes) -> str:
    return hashlib.sha256(secret).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc_now() -> str:
    return _iso(datetime.now(timezone.utc))


def _aware_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _validate_timezone_name(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ContractError("invalid_context_timezone")
    if _CONTROL_RE.search(value) or _contains_unicode_control(value):
        raise ContractError("invalid_context_timezone")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ContractError("invalid_context_timezone") from exc
    return value


def _context_timezone_name(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (CONTEXT_LOCAL_TIMEZONE_SETTING,),
    ).fetchone()
    if not row:
        raise ContractError("context_timezone_missing")
    value = str(row["value"] if hasattr(row, "keys") else row[0])
    return _validate_timezone_name(value)


def _experiment_timezone_name(value: Any) -> str:
    if value is None or not str(value):
        raise ContractError("experiment_timezone_missing")
    try:
        return _validate_timezone_name(str(value))
    except ContractError as exc:
        raise ContractError("invalid_experiment_timezone") from exc


def _timezone_for_name(timezone_name: str) -> ZoneInfo:
    return ZoneInfo(_validate_timezone_name(timezone_name))


def _local_retention_window(
    conn: sqlite3.Connection,
    occurred: datetime,
    retention_days: int,
    *,
    timezone_name: str = "",
) -> tuple[str, str, str, str]:
    zone_name = timezone_name or _context_timezone_name(conn)
    zone = _timezone_for_name(zone_name)
    local_date = occurred.astimezone(zone).date()
    expires_local_date = local_date + timedelta(days=int(retention_days))
    expires_at = datetime.combine(expires_local_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    return local_date.isoformat(), expires_local_date.isoformat(), _iso(expires_at), zone_name


def _unverified_expiry(conn: sqlite3.Connection, occurred: datetime) -> str:
    return _local_retention_window(conn, occurred, UNVERIFIED_RETENTION_DAYS)[2]


def _verified_expiry(conn: sqlite3.Connection, occurred: datetime) -> str:
    return _local_retention_window(conn, occurred, VERIFIED_RETENTION_DAYS)[2]


def _today_and_yesterday_cutoff(conn: sqlite3.Connection, now: datetime) -> datetime:
    zone = _timezone_for_name(_context_timezone_name(conn))
    local_date = now.astimezone(zone).date()
    yesterday_start = datetime.combine(local_date - timedelta(days=1), time.min, tzinfo=zone)
    return yesterday_start.astimezone(timezone.utc)


def _local_today(
    conn: sqlite3.Connection,
    now: datetime | None = None,
    *,
    timezone_name: str = "",
) -> date:
    zone_name = timezone_name or _context_timezone_name(conn)
    return _aware_utc(now).astimezone(_timezone_for_name(zone_name)).date()


def _count_query(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> int:
    row = conn.execute(sql, tuple(params)).fetchone()
    return int((row[0] if row and not hasattr(row, "keys") else row[list(row.keys())[0]]) or 0) if row else 0


def _unknown_status(reason_code: str, observed_at: str) -> dict[str, Any]:
    return {
        "status": "unknown",
        "reason_code": reason_code,
        "observed_at": observed_at,
        "last_successful_read_at": None,
        "raw_access": False,
        "feature_flag_enabled": None,
        "memory_writer_enabled": False,
        "source_revocation_cascade_ready": False,
        "service_status": "unknown",
        "actions_enabled": False,
        "counts": None,
        "trusted_contexts": None,
        "bundle": None,
    }


def _result(status: str, reason_code: str) -> dict[str, Any]:
    return {"status": status, "reason_code": reason_code, "raw_access": False}



def _merge_cascade(target: dict[str, int], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, (int, bool)):
            target[key] = int(target.get(key, 0)) + int(value)

def _zero_cascade() -> dict[str, int]:
    return {
        "observations_revoked": 0,
        "evidence_revoked": 0,
        "candidates_revoked": 0,
        "verified_contexts_revoked": 0,
        "memory_candidates_revoked": 0,
        "bundle_members_revoked": 0,
        "withdrawal_receipts_created": 0,
        "delivery_failures_recorded": 0,
        "source_event_tombstones_created": 0,
        "document_source_versions_revoked": 0,
        "supplemental_evidence_links_removed": 0,
        "confirmation_links_revoked": 0,
    }
