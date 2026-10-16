from __future__ import annotations

import sqlite3
import json
import tempfile
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from contextlib import contextmanager
from contextvars import ContextVar


SCHEMA_VERSION = 32
SQLITE_BUSY_TIMEOUT_SECONDS = 30
SQLITE_BUSY_TIMEOUT_MS = SQLITE_BUSY_TIMEOUT_SECONDS * 1000
SOURCE_STATUSES = ("active", "expired_deleted", "user_deleted", "revoked")
_STATUS_SCHEMA_READ = ContextVar("agi_full_status_schema_read", default=None)


def _schema_read_identity(conn):
    return (conn.total_changes,
            conn.execute("PRAGMA schema_version").fetchone()[0],
            conn.execute("PRAGMA data_version").fetchone()[0],
            _current_schema_version(conn))


@contextmanager
def full_status_schema_read(conn):
    """Reuse only a completed no-op initialization within one status request.

    Ordinary initialization, migrations and action entrypoints keep their
    existing behavior. Writes, other commits and DDL invalidate the committed
    observation. A caller transaction can consume it under the writer barrier,
    but can never create a new cached observation from pending changes.
    """
    token = _STATUS_SCHEMA_READ.set({"connection": conn, "identity": None})
    try:
        yield
    finally:
        _STATUS_SCHEMA_READ.reset(token)


def _execute_sql_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute a SQL script without releasing an existing writer barrier."""

    statement = ""
    for character in script:
        statement += character
        if character == ";" and sqlite3.complete_statement(statement):
            sql = statement.strip()
            statement = ""
            if sql:
                conn.execute(sql)
    if statement.strip():
        raise RuntimeError("incomplete_storage_schema_statement")


def init_schema(
    conn: sqlite3.Connection,
    *,
    candidate_rebuild_fault: Callable[[str], None] | None = None,
) -> None:
    """Initialize or migrate the schema in one explicit SQLite transaction.

    When ``ensure_ready`` already owns the schema-28/29 writer barrier this
    function participates in that transaction and never commits it.  Standalone
    callers receive the same atomicity through a local ``BEGIN IMMEDIATE``.
    """

    _refuse_schema_downgrade(conn)
    scope = _STATUS_SCHEMA_READ.get()
    reusable = (scope is not None and scope["connection"] is conn
                and candidate_rebuild_fault is None)
    stamp_allowed = reusable and not conn.in_transaction
    before = _schema_read_identity(conn) if reusable else None
    if reusable and scope["identity"] == before:
        cached = scope["identity"]
        scope["identity"] = None
        # Keep the original writer barrier around the decision too: a second
        # connection must not change a protected flag between the identity
        # check and our return. No tables are changed on this fast path.
        existing_transaction = conn.in_transaction
        if existing_transaction:
            # A no-op INSERT in a defaults reader may have opened a caller
            # transaction. Consume only the earlier *committed* stamp; obtain
            # a writer barrier without changing rows or ending that transaction.
            conn.execute("UPDATE meta SET value=value WHERE 0")
        else:
            conn.execute("BEGIN IMMEDIATE")
        try:
            _refuse_schema_downgrade(conn)
            if _schema_read_identity(conn) == cached:
                if not existing_transaction:
                    conn.commit()
                scope["identity"] = cached
                return
            if not existing_transaction:
                conn.rollback()
        except Exception:
            if not existing_transaction and conn.in_transaction:
                conn.rollback()
            raise
    if scope is not None and scope["connection"] is conn:
        scope["identity"] = None
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        _init_schema_locked(
            conn,
            candidate_rebuild_fault=candidate_rebuild_fault,
        )
        if started_transaction:
            conn.commit()
        if stamp_allowed and not conn.in_transaction and before == _schema_read_identity(conn):
            scope["identity"] = before
    except Exception:
        if started_transaction and conn.in_transaction:
            conn.rollback()
        raise


def connect(db_path: str | Path, *, busy_timeout_seconds: float = SQLITE_BUSY_TIMEOUT_SECONDS) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(path.parent)
    busy_timeout_ms = int(busy_timeout_seconds * 1000)
    conn = sqlite3.connect(path, timeout=busy_timeout_seconds)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    harden_sqlite_permissions(path)
    return conn


def connect_readonly(
    db_path: str | Path,
    *,
    busy_timeout_seconds: float = SQLITE_BUSY_TIMEOUT_SECONDS,
) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    uri = f"file:{path}?mode=ro"
    busy_timeout_ms = max(0, int(float(busy_timeout_seconds) * 1000))
    conn = sqlite3.connect(uri, uri=True, timeout=max(0.0, float(busy_timeout_seconds)))
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    conn.execute("PRAGMA query_only = ON")
    return conn


def _init_schema_locked(
    conn: sqlite3.Connection,
    *,
    candidate_rebuild_fault: Callable[[str], None] | None = None,
) -> None:
    _execute_sql_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        -- Keep the v24 database fail-closed even when a pre-v24 core opens it.
        -- The old core uses INSERT OR REPLACE and has no Python-side downgrade
        -- check, so this guard must live in the database itself.
        CREATE TRIGGER IF NOT EXISTS pam_schema24_guard_insert
        BEFORE INSERT ON meta
        WHEN NEW.key = 'schema_version'
          AND CAST(NEW.value AS INTEGER) < 24
          AND COALESCE(
                (SELECT CAST(value AS INTEGER) FROM meta WHERE key = 'schema_version'),
                0
              ) >= 24
        BEGIN
          SELECT RAISE(
            ABORT,
            'PAM schema 24 requires restoring the schema-23 backup before older core use'
          );
        END;

        CREATE TRIGGER IF NOT EXISTS pam_schema24_guard_update
        BEFORE UPDATE ON meta
        WHEN OLD.key = 'schema_version'
          AND CAST(OLD.value AS INTEGER) >= 24
          AND (
            NEW.key <> 'schema_version'
            OR CAST(NEW.value AS INTEGER) < 24
          )
        BEGIN
          SELECT RAISE(
            ABORT,
            'PAM schema 24 requires restoring the schema-23 backup before older core use'
          );
        END;

        -- v25 adds the local activity-occurrence confirmation and calibration
        -- loop.  Keep newer databases fail-closed when an older core opens the
        -- same file, just as the v24 document-tracking guard does.
        CREATE TRIGGER IF NOT EXISTS pam_schema25_guard_insert
        BEFORE INSERT ON meta
        WHEN NEW.key = 'schema_version'
          AND CAST(NEW.value AS INTEGER) < 25
          AND COALESCE(
                (SELECT CAST(value AS INTEGER) FROM meta WHERE key = 'schema_version'),
                0
              ) >= 25
        BEGIN
          SELECT RAISE(
            ABORT,
            'PAM schema 25 requires restoring the schema-24 backup before older core use'
          );
        END;

        CREATE TRIGGER IF NOT EXISTS pam_schema25_guard_update
        BEFORE UPDATE ON meta
        WHEN OLD.key = 'schema_version'
          AND CAST(OLD.value AS INTEGER) >= 25
          AND (
            NEW.key <> 'schema_version'
            OR CAST(NEW.value AS INTEGER) < 25
          )
        BEGIN
          SELECT RAISE(
            ABORT,
            'PAM schema 25 requires restoring the schema-24 backup before older core use'
          );
        END;

        -- v26 keeps source-bound confirmation rows while adding a derived
        -- question-group contract. Older cores must restore the schema-25
        -- backup instead of silently relabelling this database.
        CREATE TRIGGER IF NOT EXISTS pam_schema26_guard_insert
        BEFORE INSERT ON meta
        WHEN NEW.key = 'schema_version'
          AND CAST(NEW.value AS INTEGER) < 26
          AND COALESCE(
                (SELECT CAST(value AS INTEGER) FROM meta WHERE key = 'schema_version'),
                0
              ) >= 26
        BEGIN
          SELECT RAISE(
            ABORT,
            'PAM schema 26 requires restoring the schema-25 backup before older core use'
          );
        END;

        CREATE TRIGGER IF NOT EXISTS pam_schema26_guard_update
        BEFORE UPDATE ON meta
        WHEN OLD.key = 'schema_version'
          AND CAST(OLD.value AS INTEGER) >= 26
          AND (
            NEW.key <> 'schema_version'
            OR CAST(NEW.value AS INTEGER) < 26
          )
        BEGIN
          SELECT RAISE(
            ABORT,
            'PAM schema 26 requires restoring the schema-25 backup before older core use'
          );
        END;

        -- v27 retains local screenshot evidence only while its confirmation
        -- remains unresolved. Older cores do not understand that lifecycle,
        -- so they must restore the schema-26 rollback copy before use.
        CREATE TRIGGER IF NOT EXISTS pam_schema27_guard_insert
        BEFORE INSERT ON meta
        WHEN NEW.key = 'schema_version'
          AND CAST(NEW.value AS INTEGER) < 27
          AND COALESCE(
                (SELECT CAST(value AS INTEGER) FROM meta WHERE key = 'schema_version'),
                0
              ) >= 27
        BEGIN
          SELECT RAISE(
            ABORT,
            'PAM schema 27 requires restoring the schema-26 backup before older core use'
          );
        END;

        CREATE TRIGGER IF NOT EXISTS pam_schema27_guard_update
        BEFORE UPDATE ON meta
        WHEN OLD.key = 'schema_version'
          AND CAST(OLD.value AS INTEGER) >= 27
          AND (
            NEW.key <> 'schema_version'
            OR CAST(NEW.value AS INTEGER) < 27
          )
        BEGIN
          SELECT RAISE(
            ABORT,
            'PAM schema 27 requires restoring the schema-26 backup before older core use'
          );
        END;

        -- v28 adds source-associated business episodes, explicit answer
        -- enrichment, and content-free settlement metadata. Older cores do not
        -- understand those facts and must restore the schema-27 rollback copy.
        CREATE TRIGGER IF NOT EXISTS pam_schema28_guard_insert
        BEFORE INSERT ON meta
        WHEN NEW.key = 'schema_version'
          AND CAST(NEW.value AS INTEGER) < 28
          AND COALESCE(
                (SELECT CAST(value AS INTEGER) FROM meta WHERE key = 'schema_version'),
                0
              ) >= 28
        BEGIN
          SELECT RAISE(
            ABORT,
            'PAM schema 28 requires restoring the schema-27 backup before older core use'
          );
        END;

        CREATE TRIGGER IF NOT EXISTS pam_schema28_guard_update
        BEFORE UPDATE ON meta
        WHEN OLD.key = 'schema_version'
          AND CAST(OLD.value AS INTEGER) >= 28
          AND (
            NEW.key <> 'schema_version'
            OR CAST(NEW.value AS INTEGER) < 28
          )
        BEGIN
          SELECT RAISE(
            ABORT,
            'PAM schema 28 requires restoring the schema-27 backup before older core use'
          );
        END;

        -- v0.4 shadow already occupied schema 29. The first personal-context
        -- console schema therefore landed on schema 30 and must never be
        -- relabelled by an older core. Restore the schema-28/29 context backup
        -- instead.
        CREATE TRIGGER IF NOT EXISTS pam_schema30_guard_insert
        BEFORE INSERT ON meta
        WHEN NEW.key = 'schema_version'
          AND CAST(NEW.value AS INTEGER) < 30
          AND COALESCE(
                (SELECT CAST(value AS INTEGER) FROM meta WHERE key = 'schema_version'),
                0
              ) >= 30
        BEGIN
          SELECT RAISE(
            ABORT,
            'PAM schema 30 requires restoring the pre-context backup before older core use'
          );
        END;

        CREATE TRIGGER IF NOT EXISTS pam_schema30_guard_update
        BEFORE UPDATE ON meta
        WHEN OLD.key = 'schema_version'
          AND CAST(OLD.value AS INTEGER) >= 30
          AND (
            NEW.key <> 'schema_version'
            OR CAST(NEW.value AS INTEGER) < 30
          )
        BEGIN
          SELECT RAISE(
            ABORT,
            'PAM schema 30 requires restoring the pre-context backup before older core use'
          );
        END;

        -- Phase 2 is schema 31.  Keep the schema-30 guard for databases that
        -- have not yet initialized the two Phase-2 tables, and add a stronger
        -- one-way guard once the version reaches 31.
        CREATE TRIGGER IF NOT EXISTS pam_schema31_guard_insert
        BEFORE INSERT ON meta
        WHEN NEW.key = 'schema_version'
          AND CAST(NEW.value AS INTEGER) < 31
          AND COALESCE(
                (SELECT CAST(value AS INTEGER) FROM meta WHERE key = 'schema_version'),
                0
              ) >= 31
        BEGIN
          SELECT RAISE(
            ABORT,
            'PAM schema 31 requires restoring the pre-context backup before older core use'
          );
        END;

        CREATE TRIGGER IF NOT EXISTS pam_schema31_guard_update
        BEFORE UPDATE ON meta
        WHEN OLD.key = 'schema_version'
          AND CAST(OLD.value AS INTEGER) >= 31
          AND (
            NEW.key <> 'schema_version'
            OR CAST(NEW.value AS INTEGER) < 31
          )
        BEGIN
          SELECT RAISE(
            ABORT,
            'PAM schema 31 requires restoring the pre-context backup before older core use'
          );
        END;

        -- v0.5 adds durable, per-observation file attribution.  Older cores
        -- only understand the day-aggregate table and would otherwise relabel
        -- a v32 database as v31 while silently ignoring the evidence ledger.
        CREATE TRIGGER IF NOT EXISTS pam_schema32_guard_insert
        BEFORE INSERT ON meta
        WHEN NEW.key = 'schema_version'
          AND CAST(NEW.value AS INTEGER) < 32
          AND COALESCE(
                (SELECT CAST(value AS INTEGER) FROM meta WHERE key = 'schema_version'),
                0
              ) >= 32
        BEGIN
          SELECT RAISE(
            ABORT,
            'PAM schema 32 requires restoring the schema-31 backup before older core use'
          );
        END;

        CREATE TRIGGER IF NOT EXISTS pam_schema32_guard_update
        BEFORE UPDATE ON meta
        WHEN OLD.key = 'schema_version'
          AND CAST(OLD.value AS INTEGER) >= 32
          AND (
            NEW.key <> 'schema_version'
            OR CAST(NEW.value AS INTEGER) < 32
          )
        BEGIN
          SELECT RAISE(
            ABORT,
            'PAM schema 32 requires restoring the schema-31 backup before older core use'
          );
        END;

        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daemon_state (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS system_activity_sessions (
          id TEXT PRIMARY KEY,
          started_at TEXT NOT NULL,
          ended_at TEXT,
          status TEXT NOT NULL,
          start_reason TEXT NOT NULL,
          end_reason TEXT,
          duration_seconds INTEGER DEFAULT 0,
          last_observed_at TEXT,
          coverage_ratio REAL DEFAULT 0.0,
          sample_count INTEGER DEFAULT 0,
          privacy_event_count INTEGER DEFAULT 0,
          gap_event_count INTEGER DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS system_activity_samples (
          id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          started_at TEXT NOT NULL,
          ended_at TEXT NOT NULL,
          duration_seconds INTEGER DEFAULT 0,
          display_id TEXT,
          app_name TEXT,
          domain TEXT,
          page_title TEXT,
          category TEXT,
          category_label TEXT,
          attribution_kind TEXT NOT NULL,
          source_type TEXT NOT NULL,
          source_id TEXT,
          confidence REAL DEFAULT 0.0,
          domain_trusted INTEGER DEFAULT 0,
          privacy_flags TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS system_activity_session_rollups (
          id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          rollup_kind TEXT NOT NULL,
          label TEXT NOT NULL,
          category TEXT,
          duration_seconds INTEGER DEFAULT 0,
          share REAL DEFAULT 0.0,
          sample_count INTEGER DEFAULT 0,
          confidence REAL DEFAULT 0.0,
          rank INTEGER DEFAULT 0,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS capture_fingerprints (
          display_id TEXT PRIMARY KEY,
          frame_hash TEXT NOT NULL,
          captured_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
          id TEXT PRIMARY KEY,
          started_at TEXT NOT NULL,
          ended_at TEXT,
          status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS segments (
          id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          started_at TEXT NOT NULL,
          ended_at TEXT NOT NULL,
          top_apps TEXT,
          top_domains TEXT,
          summary TEXT,
          privacy_event_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS raw_capture_jobs (
          id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          started_at TEXT NOT NULL,
          ended_at TEXT,
          display_ids TEXT,
          temp_capture_ref TEXT,
          capture_type TEXT NOT NULL,
          processing_status TEXT NOT NULL,
          privacy_status TEXT DEFAULT 'normal',
          temp_ref_cleared INTEGER DEFAULT 0,
          deleted_at TEXT
        );

        CREATE TABLE IF NOT EXISTS raw_frame_candidates (
          id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          segment_id TEXT,
          captured_at TEXT NOT NULL,
          display_id TEXT,
          temp_frame_ref TEXT,
          frame_hash TEXT,
          change_score REAL DEFAULT 0.0,
          window_context_ref TEXT,
          url_risk_tags TEXT,
          ocr_candidate_ref TEXT,
          risk_flags TEXT,
          expires_at TEXT,
          deleted_at TEXT
        );

        CREATE TABLE IF NOT EXISTS display_frame_contexts (
          id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          segment_id TEXT,
          captured_at TEXT NOT NULL,
          display_id TEXT NOT NULL,
          visible_windows TEXT,
          frontmost_window_id TEXT,
          window_bounds TEXT,
          app_contexts TEXT,
          url_contexts TEXT,
          context_confidence REAL DEFAULT 0.0,
          source TEXT,
          privacy_flags TEXT
        );

        CREATE TABLE IF NOT EXISTS frame_signals (
          id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          segment_id TEXT,
          captured_at TEXT NOT NULL,
          display_id TEXT,
          app_name TEXT,
          bundle_id TEXT,
          window_title_redacted TEXT,
          url_redacted TEXT,
          domain TEXT,
          ocr_redacted_summary TEXT,
          safe_keywords TEXT,
          privacy_flags TEXT,
          screen_region_signal TEXT,
          redaction_status TEXT DEFAULT 'redacted',
          dedupe_status TEXT DEFAULT 'unique',
          dedupe_note TEXT,
          dedupe_duplicate_lines INTEGER DEFAULT 0,
          dedupe_retained_lines INTEGER DEFAULT 0,
          dedupe_content_hash TEXT,
          dedupe_line_hashes TEXT,
          raw_ref_deleted INTEGER DEFAULT 0,
          deletion_group_id TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS document_signals (
          id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          segment_id TEXT,
          captured_at TEXT NOT NULL,
          app_name TEXT,
          file_path_hmac TEXT,
          file_name_redacted TEXT,
          file_type TEXT,
          file_modified_at TEXT,
          content_hash_at_capture TEXT,
          subsequent_mtime_event TEXT,
          visible_location_hint TEXT,
          topic_tags TEXT,
          change_summary_redacted TEXT,
          document_source_category TEXT,
          document_source_scope TEXT,
          document_subject_scope TEXT,
          document_memory_policy TEXT,
          document_classification_confidence REAL DEFAULT 0.0,
          document_classification_reasons TEXT,
          document_risk_flags TEXT,
          document_parse_status TEXT,
          document_parse_method TEXT,
          privacy_flags TEXT,
          redaction_confidence REAL DEFAULT 0.0,
          redaction_status TEXT DEFAULT 'redacted',
          semantic_match INTEGER DEFAULT 1,
          match_status TEXT DEFAULT 'matched_text',
          source TEXT,
          source_confidence REAL DEFAULT 0.0,
          deletion_group_id TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS file_watch_roots (
          root_id TEXT PRIMARY KEY,
          root_label TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          baseline_status TEXT NOT NULL DEFAULT 'pending',
          baseline_started_at TEXT,
          baseline_completed_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS file_watch_checkpoints (
          root_id TEXT PRIMARY KEY,
          last_event_id TEXT,
          coverage_status TEXT NOT NULL DEFAULT 'pending',
          event_gap_count INTEGER NOT NULL DEFAULT 0,
          benign_event_count INTEGER NOT NULL DEFAULT 0,
          coverage_started_at TEXT,
          last_event_at TEXT,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS file_watch_state (
          file_id TEXT PRIMARY KEY,
          root_id TEXT NOT NULL,
          project_label TEXT NOT NULL DEFAULT '',
          document_path_hmac TEXT NOT NULL DEFAULT '',
          file_name_redacted TEXT NOT NULL DEFAULT '',
          file_type TEXT NOT NULL DEFAULT 'unknown',
          device_id TEXT,
          inode_id TEXT,
          modified_at TEXT,
          size_bytes INTEGER NOT NULL DEFAULT 0,
          content_hmac TEXT,
          structure_hmac_json TEXT NOT NULL DEFAULT '{}',
          detail_level TEXT NOT NULL DEFAULT 'metadata_only',
          privacy_flags TEXT NOT NULL DEFAULT '[]',
          present INTEGER NOT NULL DEFAULT 1,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS file_change_events (
          id TEXT PRIMARY KEY,
          file_id TEXT NOT NULL,
          root_id TEXT NOT NULL,
          project_label TEXT NOT NULL DEFAULT '',
          document_path_hmac TEXT NOT NULL DEFAULT '',
          file_name_redacted TEXT NOT NULL DEFAULT '',
          file_type TEXT NOT NULL DEFAULT 'unknown',
          change_kind TEXT NOT NULL,
          first_changed_at TEXT NOT NULL,
          last_changed_at TEXT NOT NULL,
          version_count INTEGER NOT NULL DEFAULT 1,
          change_summary_redacted TEXT NOT NULL DEFAULT '',
          detail_level TEXT NOT NULL DEFAULT 'metadata_only',
          confidence TEXT NOT NULL DEFAULT 'confirmed_file_event',
          privacy_flags TEXT NOT NULL DEFAULT '[]',
          deletion_group_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS file_change_versions (
          id TEXT PRIMARY KEY,
          aggregate_event_id TEXT NOT NULL,
          file_id TEXT NOT NULL,
          root_id TEXT NOT NULL,
          project_label TEXT NOT NULL DEFAULT '',
          document_path_hmac TEXT NOT NULL DEFAULT '',
          file_name_redacted TEXT NOT NULL DEFAULT '',
          file_type TEXT NOT NULL DEFAULT 'unknown',
          change_kind TEXT NOT NULL,
          changed_at TEXT NOT NULL,
          content_modified_at TEXT,
          detail_level TEXT NOT NULL DEFAULT 'metadata_only',
          actor_source TEXT NOT NULL DEFAULT 'unattributed',
          actor_tool TEXT NOT NULL DEFAULT '',
          actor_confidence TEXT NOT NULL DEFAULT 'unknown',
          actor_reason_codes TEXT NOT NULL DEFAULT '[]',
          coverage_state TEXT NOT NULL DEFAULT 'continuous',
          helper_event_id TEXT NOT NULL DEFAULT '',
          observed_version_count INTEGER NOT NULL DEFAULT 1,
          deletion_group_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS file_watch_coverage_gaps (
          id TEXT PRIMARY KEY,
          root_id TEXT NOT NULL,
          observed_at TEXT NOT NULL,
          reason_code TEXT NOT NULL,
          affects_attribution INTEGER NOT NULL DEFAULT 1,
          helper_event_id TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS extracted_activities (
          id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          segment_id TEXT,
          started_at TEXT NOT NULL,
          ended_at TEXT,
          activity_summary TEXT NOT NULL,
          apps TEXT,
          domains TEXT,
          evidence_refs TEXT,
          privacy_level TEXT DEFAULT 'normal',
          redaction_confidence REAL DEFAULT 0.0,
          quarantine_status TEXT DEFAULT 'review_required',
          quarantine_reason TEXT,
          quarantine_expires_at TEXT,
          dedupe_status TEXT DEFAULT 'unique',
          dedupe_note TEXT,
          dedupe_duplicate_lines INTEGER DEFAULT 0,
          dedupe_retained_lines INTEGER DEFAULT 0,
          dedupe_content_hash TEXT,
          deletion_group_id TEXT NOT NULL,
          deleted_at TEXT
        );

        CREATE TABLE IF NOT EXISTS activity_provenance (
          id TEXT PRIMARY KEY,
          derived_type TEXT NOT NULL,
          derived_id TEXT NOT NULL,
          source_type TEXT NOT NULL,
          source_id TEXT NOT NULL,
          source_time_range TEXT,
          deletion_group_id TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS privacy_events (
          id TEXT PRIMARY KEY,
          started_at TEXT NOT NULL,
          ended_at TEXT,
          type TEXT NOT NULL,
          reason TEXT,
          app_name TEXT,
          domain TEXT,
          action TEXT NOT NULL,
          deletion_group_id TEXT
        );

        CREATE TABLE IF NOT EXISTS gap_events (
          id TEXT PRIMARY KEY,
          started_at TEXT NOT NULL,
          ended_at TEXT,
          reason TEXT NOT NULL,
          display_ids TEXT,
          raw_deleted_at TEXT,
          detail TEXT,
          deletion_group_id TEXT
        );

        CREATE TABLE IF NOT EXISTS duplicate_events (
          id TEXT PRIMARY KEY,
          started_at TEXT NOT NULL,
          ended_at TEXT,
          display_id TEXT,
          reason TEXT NOT NULL,
          duplicate_count INTEGER DEFAULT 1,
          source_activity_id TEXT,
          note TEXT,
          deletion_group_id TEXT
        );

        CREATE TABLE IF NOT EXISTS capture_coverage_ledger (
          id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          segment_id TEXT,
          display_id TEXT,
          started_at TEXT NOT NULL,
          ended_at TEXT NOT NULL,
          capture_mode TEXT NOT NULL,
          coverage_ratio REAL DEFAULT 0.0,
          frames_sampled INTEGER DEFAULT 0,
          frames_dropped INTEGER DEFAULT 0,
          raw_jobs_created INTEGER DEFAULT 0,
          raw_jobs_deleted INTEGER DEFAULT 0,
          clear_count INTEGER DEFAULT 0,
          partial_redacted_count INTEGER DEFAULT 0,
          review_required_count INTEGER DEFAULT 0,
          blocked_count INTEGER DEFAULT 0,
          gap_count INTEGER DEFAULT 0,
          duplicate_exact_count INTEGER DEFAULT 0,
          partial_duplicate_count INTEGER DEFAULT 0,
          vlm_review_count INTEGER DEFAULT 0,
          vlm_cache_hit_count INTEGER DEFAULT 0,
          vlm_clear_count INTEGER DEFAULT 0,
          vlm_partial_redacted_count INTEGER DEFAULT 0,
          vlm_blocked_count INTEGER DEFAULT 0,
          vlm_review_required_count INTEGER DEFAULT 0,
          vlm_timeout_count INTEGER DEFAULT 0,
          vlm_unavailable_count INTEGER DEFAULT 0,
          vlm_invalid_count INTEGER DEFAULT 0,
          vlm_disabled_count INTEGER DEFAULT 0,
          vlm_last_status TEXT,
          vlm_model TEXT,
          vlm_revision_key TEXT,
          vlm_canary_status TEXT,
          vlm_elapsed_ms INTEGER DEFAULT 0,
          vlm_canary_elapsed_ms INTEGER DEFAULT 0,
          vlm_image_downscaled INTEGER DEFAULT 0,
          vlm_image_original_bytes INTEGER DEFAULT 0,
          vlm_image_submitted_bytes INTEGER DEFAULT 0,
          degraded_reason TEXT
        );

        CREATE TABLE IF NOT EXISTS vlm_pending_jobs (
          id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          captured_at TEXT NOT NULL,
          display_id TEXT,
          frame_hash TEXT,
          temp_frame_ref TEXT,
          temp_ref_bytes INTEGER DEFAULT 0,
          event_json TEXT NOT NULL,
          priority INTEGER DEFAULT 50,
          status TEXT NOT NULL,
          attempt_count INTEGER DEFAULT 0,
          expires_at TEXT NOT NULL,
          processing_started_at TEXT,
          next_retry_at TEXT,
          completed_at TEXT,
          result_status TEXT,
          dropped_reason TEXT,
          last_error TEXT,
          temp_ref_deleted INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS distribution_packages (
          id TEXT PRIMARY KEY,
          target TEXT NOT NULL,
          date_range TEXT NOT NULL,
          format TEXT NOT NULL,
          redaction_policy TEXT,
          source TEXT DEFAULT 'extracted_only',
          raw_access INTEGER DEFAULT 0,
          generation_mode TEXT DEFAULT 'on_demand',
          cache_ref TEXT,
          cache_ttl_seconds INTEGER DEFAULT 0,
          cache_expires_at TEXT,
          cache_invalidated_at TEXT,
          privacy_firewall_version TEXT,
          redaction_policy_version TEXT,
          memory_write_policy TEXT DEFAULT 'ephemeral_only',
          capability_scope TEXT DEFAULT 'read_activity',
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ha_source_records (
          id TEXT PRIMARY KEY,
          package_id TEXT NOT NULL,
          source_status TEXT NOT NULL,
          source_device TEXT NOT NULL,
          source_type TEXT NOT NULL,
          time_range TEXT NOT NULL,
          coverage_rate REAL DEFAULT 0.0,
          retention_until TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ha_automation_jobs (
          id TEXT PRIMARY KEY,
          hour_key TEXT NOT NULL UNIQUE,
          started_at TEXT NOT NULL,
          ended_at TEXT NOT NULL,
          package_id TEXT,
          activity_count INTEGER DEFAULT 0,
          safe_timeline_event_count INTEGER DEFAULT 0,
          trigger_mode TEXT NOT NULL,
          hermes_profile TEXT NOT NULL,
          status TEXT NOT NULL,
          attempt_count INTEGER DEFAULT 0,
          last_error TEXT,
          command TEXT,
          generated_at TEXT,
          triggered_at TEXT,
          completed_at TEXT,
          processing_started_at TEXT,
          next_retry_at TEXT,
          dead_letter_reason TEXT,
          hermes_result_status TEXT,
          memories_written_count INTEGER DEFAULT 0,
          skipped_reason TEXT,
          hermes_result_json TEXT,
          written_candidate_count INTEGER DEFAULT 0,
          skipped_candidate_count INTEGER DEFAULT 0,
          written_candidate_types TEXT,
          skipped_reasons TEXT,
          write_audit_summary TEXT,
          written_memory_summaries TEXT,
          memory_report_status TEXT,
          memory_report_target TEXT,
          memory_report_error TEXT,
          memory_reported_at TEXT,
          memory_report_attempt_count INTEGER DEFAULT 0,
          memory_report_next_retry_at TEXT,
          candidate_count INTEGER DEFAULT 0,
          candidate_skip_reason TEXT,
          eligible_candidate_count INTEGER DEFAULT 0,
          review_only_candidate_count INTEGER DEFAULT 0,
          ephemeral_candidate_count INTEGER DEFAULT 0,
          confirmed_action_count INTEGER DEFAULT 0,
          context_clue_count INTEGER DEFAULT 0,
          noise_or_reference_count INTEGER DEFAULT 0,
          single_evidence_eligible_count INTEGER DEFAULT 0,
          subject_risk_candidate_count INTEGER DEFAULT 0,
          single_evidence_demoted_count INTEGER DEFAULT 0,
          media_or_file_listing_ephemeral_count INTEGER DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS quality_feedback (
          id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          target_type TEXT NOT NULL,
          target_id TEXT NOT NULL,
          target_started_at TEXT,
          target_ended_at TEXT,
          issue_type TEXT NOT NULL,
          expected_status TEXT NOT NULL,
          note_redacted TEXT,
          deletion_group_id TEXT
        );

        CREATE TABLE IF NOT EXISTS quality_advice_runs (
          id TEXT PRIMARY KEY,
          run_key TEXT NOT NULL UNIQUE,
          started_at TEXT NOT NULL,
          ended_at TEXT NOT NULL,
          profile TEXT NOT NULL,
          status TEXT NOT NULL,
          package_id TEXT,
          memory_candidate_count INTEGER DEFAULT 0,
          document_candidate_count INTEGER DEFAULT 0,
          document_match_count INTEGER DEFAULT 0,
          issue_count INTEGER DEFAULT 0,
          recommendation_count INTEGER DEFAULT 0,
          summary_redacted TEXT,
          advice_json TEXT,
          last_error TEXT,
          created_at TEXT NOT NULL,
          completed_at TEXT,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_decisions (
          item_key TEXT PRIMARY KEY,
          item_type TEXT NOT NULL,
          source_kind TEXT NOT NULL,
          action TEXT NOT NULL,
          safe_summary TEXT NOT NULL,
          user_note TEXT,
          decision_signature TEXT NOT NULL,
          generalize_scope TEXT NOT NULL,
          applied_plan_id TEXT,
          source_package_id TEXT,
          source_activity_ids TEXT NOT NULL DEFAULT '[]',
          source_start TEXT,
          source_end TEXT,
          deletion_group_ids TEXT NOT NULL DEFAULT '[]',
          provenance_status TEXT NOT NULL DEFAULT 'legacy_unknown',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS activity_confirmation_items (
          item_key TEXT PRIMARY KEY,
          event_type TEXT NOT NULL,
          safe_observation TEXT NOT NULL,
          decision_question TEXT NOT NULL,
          why_uncertain TEXT NOT NULL,
          app_family TEXT NOT NULL DEFAULT '',
          observed_start TEXT NOT NULL,
          observed_end TEXT NOT NULL,
          confidence REAL NOT NULL DEFAULT 0.0,
          reason_codes TEXT NOT NULL DEFAULT '[]',
          region_kind TEXT NOT NULL DEFAULT 'unknown',
          actionability TEXT NOT NULL DEFAULT 'unknown',
          source_activity_ids TEXT NOT NULL DEFAULT '[]',
          deletion_group_ids TEXT NOT NULL DEFAULT '[]',
          fingerprint TEXT NOT NULL,
          calibration_key TEXT NOT NULL,
          group_key TEXT NOT NULL DEFAULT '',
          group_signature TEXT NOT NULL DEFAULT '',
          subject TEXT NOT NULL DEFAULT '{}',
          evidence TEXT NOT NULL DEFAULT '',
          cleaned_content TEXT NOT NULL DEFAULT '',
          clarification TEXT NOT NULL DEFAULT '{}',
          clarification_note TEXT NOT NULL DEFAULT '',
          question_kind TEXT NOT NULL DEFAULT 'occurrence',
          candidate_origin TEXT NOT NULL DEFAULT 'explicit_claim',
          priority TEXT NOT NULL DEFAULT 'normal',
          episode_id TEXT NOT NULL DEFAULT '',
          safe_signal_types TEXT NOT NULL DEFAULT '[]',
          suggested_workstream TEXT NOT NULL DEFAULT '',
          suggested_outcome TEXT NOT NULL DEFAULT 'unknown',
          user_workstream TEXT NOT NULL DEFAULT '',
          user_outcome TEXT NOT NULL DEFAULT '',
          notification_eligible INTEGER NOT NULL DEFAULT 0,
          state TEXT NOT NULL DEFAULT 'pending',
          answer TEXT,
          answer_reason TEXT,
          user_note TEXT NOT NULL DEFAULT '',
          generalize_scope TEXT NOT NULL DEFAULT 'exact',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          answered_at TEXT
        );

        CREATE TABLE IF NOT EXISTS activity_confirmation_evidence_files (
          evidence_id TEXT PRIMARY KEY,
          source_frame_id TEXT NOT NULL,
          captured_at TEXT NOT NULL,
          display_id TEXT NOT NULL DEFAULT '',
          file_name TEXT NOT NULL DEFAULT '',
          content_sha256 TEXT NOT NULL DEFAULT '',
          byte_size INTEGER NOT NULL DEFAULT 0,
          state TEXT NOT NULL DEFAULT 'unavailable',
          backup_excluded INTEGER NOT NULL DEFAULT 0,
          last_error TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          deleted_at TEXT,
          delete_reason TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS activity_confirmation_evidence_refs (
          evidence_id TEXT NOT NULL,
          item_key TEXT NOT NULL,
          source_activity_id TEXT NOT NULL DEFAULT '',
          deletion_group_id TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          released_at TEXT,
          release_reason TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(evidence_id, item_key),
          FOREIGN KEY(evidence_id) REFERENCES activity_confirmation_evidence_files(evidence_id)
            ON DELETE CASCADE,
          FOREIGN KEY(item_key) REFERENCES activity_confirmation_items(item_key)
            ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS activity_confirmation_calibrations (
          calibration_key TEXT PRIMARY KEY,
          event_type TEXT NOT NULL,
          app_family TEXT NOT NULL DEFAULT '',
          region_kind TEXT NOT NULL DEFAULT 'unknown',
          actionability TEXT NOT NULL DEFAULT 'unknown',
          positive_count INTEGER NOT NULL DEFAULT 0,
          negative_count INTEGER NOT NULL DEFAULT 0,
          misrecognized_count INTEGER NOT NULL DEFAULT 0,
          reason_counts TEXT NOT NULL DEFAULT '{}',
          last_answer TEXT,
          last_reason TEXT,
          revoked_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS activity_confirmation_settlements (
          report_date TEXT PRIMARY KEY,
          revision TEXT NOT NULL,
          settled_at TEXT NOT NULL,
          completeness_status TEXT NOT NULL,
          pending_total INTEGER NOT NULL DEFAULT 0,
          pending_high_priority INTEGER NOT NULL DEFAULT 0,
          pending_by_kind TEXT NOT NULL DEFAULT '{}',
          candidate_funnel TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS session_transition_evidence (
          id TEXT PRIMARY KEY,
          observed_at TEXT NOT NULL,
          previous_state TEXT NOT NULL,
          state TEXT NOT NULL,
          on_console INTEGER,
          source TEXT NOT NULL,
          reason TEXT NOT NULL,
          capture_allowed INTEGER NOT NULL DEFAULT 0,
          transition_kind TEXT NOT NULL,
          daemon_start INTEGER NOT NULL DEFAULT 0,
          last_valid_state TEXT,
          last_valid_at TEXT
        );

        CREATE TABLE IF NOT EXISTS soak_capture_cycles (
          id TEXT PRIMARY KEY,
          observed_at TEXT NOT NULL,
          status TEXT NOT NULL,
          captured_count INTEGER NOT NULL DEFAULT 0,
          skipped_count INTEGER NOT NULL DEFAULT 0,
          error_count INTEGER NOT NULL DEFAULT 0,
          session_state TEXT NOT NULL,
          raw_residue_count INTEGER NOT NULL DEFAULT 0,
          unresolved_raw_jobs INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS soak_resource_samples (
          id TEXT PRIMARY KEY,
          observed_at TEXT NOT NULL,
          cpu_percent REAL,
          rss_bytes INTEGER,
          disk_bytes INTEGER,
          core_version TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS soak_resource_baselines (
          id TEXT PRIMARY KEY,
          recorded_at TEXT NOT NULL,
          source_version TEXT NOT NULL,
          cpu_percent REAL,
          rss_bytes INTEGER,
          disk_bytes INTEGER
        );

        CREATE TABLE IF NOT EXISTS soak_evidence_events (
          id TEXT PRIMARY KEY,
          occurred_at TEXT NOT NULL,
          event_type TEXT NOT NULL,
          result TEXT NOT NULL,
          minutes_saved INTEGER NOT NULL DEFAULT 0,
          source TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS soak_daily_snapshots (
          day TEXT PRIMARY KEY,
          generated_at TEXT NOT NULL,
          status TEXT NOT NULL,
          payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS source_revocation_events (
          id TEXT PRIMARY KEY,
          package_id TEXT NOT NULL,
          source_status TEXT NOT NULL,
          time_range TEXT NOT NULL,
          reason TEXT NOT NULL,
          deleted_at TEXT NOT NULL,
          deletion_group_ids TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
          id TEXT PRIMARY KEY,
          at TEXT NOT NULL,
          action TEXT NOT NULL,
          target_type TEXT,
          target_id TEXT,
          deletion_group_id TEXT,
          detail TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
          entity_type UNINDEXED,
          entity_id UNINDEXED,
          started_at UNINDEXED,
          app_name,
          domain,
          summary,
          keywords
        );

        CREATE INDEX IF NOT EXISTS idx_segments_time ON segments(started_at, ended_at);
        CREATE INDEX IF NOT EXISTS idx_system_activity_sessions_time ON system_activity_sessions(started_at, ended_at);
        CREATE INDEX IF NOT EXISTS idx_system_activity_sessions_status ON system_activity_sessions(status, started_at);
        CREATE INDEX IF NOT EXISTS idx_system_activity_samples_session ON system_activity_samples(session_id, attribution_kind);
        CREATE INDEX IF NOT EXISTS idx_system_activity_samples_time ON system_activity_samples(started_at, ended_at);
        CREATE INDEX IF NOT EXISTS idx_system_activity_rollups_session ON system_activity_session_rollups(session_id, rollup_kind, rank);
        CREATE INDEX IF NOT EXISTS idx_frame_signals_time ON frame_signals(captured_at);
        CREATE INDEX IF NOT EXISTS idx_document_signals_time ON document_signals(captured_at);
        CREATE INDEX IF NOT EXISTS idx_document_signals_deletion_group ON document_signals(deletion_group_id);
        CREATE INDEX IF NOT EXISTS idx_file_watch_state_root ON file_watch_state(root_id, present);
        CREATE INDEX IF NOT EXISTS idx_file_watch_state_inode ON file_watch_state(root_id, device_id, inode_id);
        CREATE INDEX IF NOT EXISTS idx_file_change_events_time ON file_change_events(first_changed_at, last_changed_at);
        CREATE INDEX IF NOT EXISTS idx_file_change_events_file ON file_change_events(file_id, last_changed_at);
        CREATE INDEX IF NOT EXISTS idx_file_change_events_deletion_group ON file_change_events(deletion_group_id);
        CREATE INDEX IF NOT EXISTS idx_file_change_versions_time ON file_change_versions(changed_at);
        CREATE INDEX IF NOT EXISTS idx_file_change_versions_file ON file_change_versions(file_id, changed_at);
        CREATE INDEX IF NOT EXISTS idx_file_change_versions_actor ON file_change_versions(actor_source, changed_at);
        CREATE INDEX IF NOT EXISTS idx_file_change_versions_aggregate ON file_change_versions(aggregate_event_id);
        CREATE INDEX IF NOT EXISTS idx_file_change_versions_deletion_group ON file_change_versions(deletion_group_id);
        CREATE INDEX IF NOT EXISTS idx_file_watch_coverage_gaps_root_time ON file_watch_coverage_gaps(root_id, observed_at);
        CREATE INDEX IF NOT EXISTS idx_file_watch_coverage_gaps_affects_attribution ON file_watch_coverage_gaps(affects_attribution);
        CREATE INDEX IF NOT EXISTS idx_extracted_activities_time ON extracted_activities(started_at, ended_at);
        CREATE INDEX IF NOT EXISTS idx_extracted_activities_deletion_group ON extracted_activities(deletion_group_id);
        CREATE INDEX IF NOT EXISTS idx_activity_provenance_derived_source
          ON activity_provenance(derived_type, derived_id, source_type, source_id);
        CREATE INDEX IF NOT EXISTS idx_privacy_events_time ON privacy_events(started_at, ended_at);
        CREATE INDEX IF NOT EXISTS idx_gap_events_time ON gap_events(started_at, ended_at);
        CREATE INDEX IF NOT EXISTS idx_duplicate_events_time ON duplicate_events(started_at, ended_at);
        CREATE INDEX IF NOT EXISTS idx_capture_coverage_day_display ON capture_coverage_ledger(datetime(started_at), display_id);
        CREATE INDEX IF NOT EXISTS idx_vlm_pending_jobs_status ON vlm_pending_jobs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_vlm_pending_jobs_time ON vlm_pending_jobs(captured_at);
        CREATE INDEX IF NOT EXISTS idx_distribution_packages_target ON distribution_packages(target);
        CREATE INDEX IF NOT EXISTS idx_ha_source_records_package ON ha_source_records(package_id);
        CREATE INDEX IF NOT EXISTS idx_ha_automation_jobs_status ON ha_automation_jobs(status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_ha_automation_jobs_package ON ha_automation_jobs(package_id);
        CREATE INDEX IF NOT EXISTS idx_quality_feedback_time ON quality_feedback(target_started_at, target_ended_at);
        CREATE INDEX IF NOT EXISTS idx_quality_feedback_target ON quality_feedback(target_type, target_id);
        CREATE INDEX IF NOT EXISTS idx_quality_advice_runs_status ON quality_advice_runs(status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_quality_advice_runs_time ON quality_advice_runs(started_at, ended_at);
        CREATE INDEX IF NOT EXISTS idx_user_decisions_action ON user_decisions(action, updated_at);
        CREATE INDEX IF NOT EXISTS idx_user_decisions_signature ON user_decisions(decision_signature, generalize_scope);
        CREATE INDEX IF NOT EXISTS idx_activity_confirmation_time ON activity_confirmation_items(observed_start, observed_end);
        CREATE INDEX IF NOT EXISTS idx_activity_confirmation_state ON activity_confirmation_items(state, observed_start);
        CREATE INDEX IF NOT EXISTS idx_activity_confirmation_fingerprint ON activity_confirmation_items(fingerprint);
        CREATE INDEX IF NOT EXISTS idx_activity_confirmation_calibration_key ON activity_confirmation_items(calibration_key);
        CREATE INDEX IF NOT EXISTS idx_activity_confirmation_evidence_state
          ON activity_confirmation_evidence_files(state, updated_at);
        CREATE INDEX IF NOT EXISTS idx_activity_confirmation_evidence_frame
          ON activity_confirmation_evidence_files(source_frame_id);
        CREATE INDEX IF NOT EXISTS idx_activity_confirmation_evidence_ref_item
          ON activity_confirmation_evidence_refs(item_key, released_at);
        CREATE INDEX IF NOT EXISTS idx_activity_confirmation_evidence_ref_source
          ON activity_confirmation_evidence_refs(source_activity_id, deletion_group_id);
        CREATE INDEX IF NOT EXISTS idx_activity_confirmation_evidence_ref_active
          ON activity_confirmation_evidence_refs(evidence_id, released_at);
        CREATE INDEX IF NOT EXISTS idx_activity_calibration_updated ON activity_confirmation_calibrations(updated_at);
        CREATE INDEX IF NOT EXISTS idx_session_transition_time ON session_transition_evidence(observed_at, state);
        CREATE INDEX IF NOT EXISTS idx_soak_capture_cycles_time ON soak_capture_cycles(observed_at, status);
        CREATE INDEX IF NOT EXISTS idx_soak_resource_samples_time ON soak_resource_samples(observed_at);
        CREATE INDEX IF NOT EXISTS idx_soak_evidence_events_time ON soak_evidence_events(occurred_at, event_type);
        """
    )
    _migrate_frame_signal_raw_default(conn)
    _migrate_frame_signal_region_columns(conn)
    _backfill_frame_signal_regions(conn)
    _migrate_source_status_values(conn)
    _migrate_capture_health_columns(conn)
    _migrate_vlm_pending_columns(conn)
    _migrate_dedupe_columns(conn)
    _migrate_document_signal_columns(conn)
    _migrate_file_watch_checkpoint_columns(conn)
    _migrate_file_change_attribution_schema(conn)
    _migrate_ha_automation_columns(conn)
    _migrate_quality_feedback_columns(conn)
    _migrate_quality_advice_columns(conn)
    _migrate_user_decision_columns(conn)
    _migrate_activity_confirmation_columns(conn)
    # Schema 31 is additive and deliberately names every new object with a
    # context_ prefix so a former v0.4 schema-29 shadow database can coexist.
    from .context_console import init_context_console_schema

    init_context_console_schema(
        conn,
        candidate_rebuild_fault=candidate_rebuild_fault,
    )
    _set_schema_version(conn)


def ensure_ready(
    db_path: str | Path,
    *,
    busy_timeout_seconds: float = SQLITE_BUSY_TIMEOUT_SECONDS,
    _migration_observer: Callable[[str], None] | None = None,
    _candidate_rebuild_fault: Callable[[str], None] | None = None,
) -> sqlite3.Connection:
    path = Path(db_path)
    conn = connect(path, busy_timeout_seconds=busy_timeout_seconds)
    try:
        _backup_schema_25_before_upgrade(conn, path)
        _backup_schema_26_before_upgrade(conn, path)
        _backup_schema_27_before_upgrade(conn, path)
        current_version = _current_schema_version(conn)
        if current_version in {"28", "29"}:
            _upgrade_context_schema_with_writer_barrier(
                conn,
                path,
                busy_timeout_seconds=busy_timeout_seconds,
                observer=_migration_observer,
                candidate_rebuild_fault=_candidate_rebuild_fault,
            )
        else:
            init_schema(conn, candidate_rebuild_fault=_candidate_rebuild_fault)
            _force_governed_writer_flags_off(conn)
            conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
        raise

    harden_sqlite_permissions(path)
    try:
        from .confirmation_evidence import reconcile_confirmation_evidence

        reconcile_confirmation_evidence(conn, path.parent)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        conn.execute(
            "UPDATE activity_confirmation_evidence_files "
            "SET last_error='startup_reconcile_failed',updated_at=datetime('now') "
            "WHERE state='delete_pending'"
        )
        conn.commit()
    return conn


def _upgrade_context_schema_with_writer_barrier(
    conn: sqlite3.Connection,
    path: Path,
    *,
    busy_timeout_seconds: float,
    observer: Callable[[str], None] | None,
    candidate_rebuild_fault: Callable[[str], None] | None,
) -> None:
    """Snapshot and migrate schema 28/29 under one continuous writer lock.

    ``BEGIN IMMEDIATE`` is the linearization point.  It waits for every earlier
    writer to commit, then prevents any later writer from entering until the
    schema migration commits or rolls back.  A second read-only connection can
    still drive ``sqlite3_backup`` without forcing the barrier connection to
    commit its transaction.
    """

    conn.execute("BEGIN IMMEDIATE")
    _notify_migration_observer(observer, "writer_barrier_acquired")
    try:
        snapshot_source = connect_readonly(
            path,
            busy_timeout_seconds=busy_timeout_seconds,
        )
        try:
            _backup_context_schema_before_upgrade(snapshot_source, path)
        finally:
            snapshot_source.close()
        _notify_migration_observer(observer, "backup_snapshot_complete")
        init_schema(conn, candidate_rebuild_fault=candidate_rebuild_fault)
        _force_governed_writer_flags_off(conn)
        _notify_migration_observer(observer, "migration_before_commit")
        conn.commit()
        _notify_migration_observer(observer, "migration_committed", suppress_errors=True)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        _notify_migration_observer(observer, "migration_rolled_back", suppress_errors=True)
        raise


def _notify_migration_observer(
    observer: Callable[[str], None] | None,
    stage: str,
    *,
    suppress_errors: bool = False,
) -> None:
    if observer is None:
        return
    if suppress_errors:
        try:
            observer(stage)
        except Exception:
            return
    else:
        observer(stage)


def _force_governed_writer_flags_off(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for key in (
        "ha_auto_package_enabled",
        "context_memory_writer_enabled",
        "context_source_revocation_cascade_ready",
    ):
        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value='false',updated_at=excluded.updated_at",
            (key, "false", now),
        )


def _current_schema_version(conn: sqlite3.Connection) -> str:
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if not has_meta:
        return ""
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return str(row["value"] or "") if row else ""



def _backup_context_schema_before_upgrade(conn: sqlite3.Connection, path: Path) -> None:
    """Create or safely reuse a WAL-consistent schema-28/29 rollback copy.

    Reuse is bound to both the current database file identity and the exact
    content digest of a fresh SQLite backup snapshot.  A same-path stale backup
    therefore cannot silently stand in for the database being migrated.
    """
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if not has_meta:
        return
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    version = str(row["value"] or "") if row else ""
    if version not in {"28", "29"}:
        return
    backup_path = path.with_name(f"{path.name}.schema{version}-context-backup")
    identity_path = backup_path.with_name(f"{backup_path.name}.identity.json")
    temporary_path: Path | None = None
    try:
        temporary_path = _create_context_backup_snapshot(conn, path, version)
        source_digest = _sha256_file(temporary_path)
        database_identity = _database_file_identity(path)
        if backup_path.exists() or backup_path.is_symlink():
            if backup_path.is_symlink() or not backup_path.is_file():
                raise RuntimeError(f"schema_{version}_context_backup_invalid")
            try:
                backup_path.chmod(0o600)
            except OSError as exc:
                raise RuntimeError(f"schema_{version}_context_backup_permissions_failed") from exc
            if not _valid_context_pre_upgrade_backup(backup_path, version):
                raise RuntimeError(f"schema_{version}_context_backup_invalid")
            metadata = _read_context_backup_identity(identity_path)
            backup_digest = _sha256_file(backup_path)
            expected = {
                "format_version": 1,
                "schema_version": version,
                "database_identity": database_identity,
                "source_content_sha256": source_digest,
                "backup_content_sha256": backup_digest,
            }
            if metadata != expected or backup_digest != source_digest:
                raise RuntimeError(f"schema_{version}_context_backup_stale")
            temporary_path.unlink(missing_ok=True)
            return

        if identity_path.exists() or identity_path.is_symlink():
            # An orphan sidecar may belong to a prior database at this path.
            # Never overwrite it while creating the only rollback snapshot.
            raise RuntimeError(f"schema_{version}_context_backup_identity_orphaned")
        temporary_path.chmod(0o600)
        try:
            # A same-directory hard link is an atomic no-clobber publish.  If a
            # concurrent/stale file appears after the existence check, link()
            # fails instead of replacing the only rollback copy.
            os.link(temporary_path, backup_path, follow_symlinks=False)
        except FileExistsError as exc:
            raise RuntimeError(f"schema_{version}_context_backup_already_exists") from exc
        temporary_path.unlink()
        temporary_path = None
        backup_path.chmod(0o600)
        if not _valid_context_pre_upgrade_backup(backup_path, version):
            raise RuntimeError(f"schema_{version}_context_backup_invalid")
        backup_digest = _sha256_file(backup_path)
        metadata = {
            "format_version": 1,
            "schema_version": version,
            "database_identity": database_identity,
            "source_content_sha256": source_digest,
            "backup_content_sha256": backup_digest,
        }
        _write_context_backup_identity(identity_path, metadata)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"schema_{version}_context_backup_failed") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _create_context_backup_snapshot(
    conn: sqlite3.Connection,
    path: Path,
    version: str,
) -> Path:
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.name}.schema{version}-context-snapshot-",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        backup_conn = sqlite3.connect(temporary_path)
        try:
            conn.backup(backup_conn)
        finally:
            backup_conn.close()
        temporary_path.chmod(0o600)
        return temporary_path
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _database_file_identity(path: Path) -> str:
    stat = path.stat()
    material = f"{int(stat.st_dev)}:{int(stat.st_ino)}".encode("ascii")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _read_context_backup_identity(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
        raise RuntimeError("context_backup_identity_invalid")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("context_backup_identity_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "format_version",
        "schema_version",
        "database_identity",
        "source_content_sha256",
        "backup_content_sha256",
    }:
        raise RuntimeError("context_backup_identity_invalid")
    return payload


def _write_context_backup_identity(path: Path, payload: dict[str, object]) -> None:
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            mode="w",
            encoding="utf-8",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path = Path(temporary_name)
        temporary_path.chmod(0o600)
        try:
            os.link(temporary_path, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise RuntimeError("context_backup_identity_already_exists") from exc
        temporary_path.unlink()
        temporary_name = ""
        path.chmod(0o600)
    except Exception:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise


def _valid_context_pre_upgrade_backup(path: Path, expected_version: str) -> bool:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
            return False
        backup = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            integrity = backup.execute("PRAGMA quick_check").fetchone()
            version = backup.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            return bool(
                integrity
                and str(integrity[0] or "").lower() == "ok"
                and version
                and str(version[0] or "") == expected_version
            )
        finally:
            backup.close()
    except (OSError, sqlite3.Error):
        return False


def _backup_schema_25_before_upgrade(conn: sqlite3.Connection, path: Path) -> None:
    """Create one WAL-consistent rollback copy before the schema-26 upgrade."""
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if not has_meta:
        return
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if not row or str(row["value"] or "") != "25":
        return
    backup_path = path.with_name(f"{path.name}.schema25-backup")
    if backup_path.exists():
        if _valid_schema_25_backup(backup_path):
            return
        raise RuntimeError("schema_25_backup_invalid")
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.schema25-backup-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
        backup_conn = sqlite3.connect(temporary_name)
        try:
            conn.backup(backup_conn)
        finally:
            backup_conn.close()
        temporary_path = Path(temporary_name)
        temporary_path.chmod(0o600)
        temporary_path.replace(backup_path)
        backup_path.chmod(0o600)
        if not _valid_schema_25_backup(backup_path):
            raise RuntimeError("schema_25_backup_invalid")
    except Exception as exc:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise RuntimeError("schema_25_backup_failed") from exc


def _valid_schema_25_backup(path: Path) -> bool:
    try:
        # sqlite3_backup() can preserve WAL journal mode in the single-file
        # snapshot even though the snapshot has no WAL sidecar. Immutable
        # read-only mode validates the fully copied database pages without
        # trying to create or discover sidecars beside the rollback artifact.
        backup = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            integrity = backup.execute("PRAGMA quick_check").fetchone()
            version = backup.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            return bool(
                integrity
                and str(integrity[0] or "").lower() == "ok"
                and version
                and str(version[0] or "") == "25"
            )
        finally:
            backup.close()
    except sqlite3.Error:
        return False


def _backup_schema_26_before_upgrade(conn: sqlite3.Connection, path: Path) -> None:
    """Create one WAL-consistent rollback copy before the schema-27 upgrade."""
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if not has_meta:
        return
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if not row or str(row["value"] or "") != "26":
        return
    backup_path = path.with_name(f"{path.name}.schema26-backup")
    if backup_path.exists():
        if _valid_schema_26_backup(backup_path):
            return
        raise RuntimeError("schema_26_backup_invalid")
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.schema26-backup-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
        backup_conn = sqlite3.connect(temporary_name)
        try:
            conn.backup(backup_conn)
        finally:
            backup_conn.close()
        temporary_path = Path(temporary_name)
        temporary_path.chmod(0o600)
        temporary_path.replace(backup_path)
        backup_path.chmod(0o600)
        if not _valid_schema_26_backup(backup_path):
            raise RuntimeError("schema_26_backup_invalid")
    except Exception as exc:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise RuntimeError("schema_26_backup_failed") from exc


def _valid_schema_26_backup(path: Path) -> bool:
    try:
        backup = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            integrity = backup.execute("PRAGMA integrity_check").fetchone()
            version = backup.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            return bool(
                integrity
                and str(integrity[0] or "").lower() == "ok"
                and version
                and str(version[0] or "") == "26"
            )
        finally:
            backup.close()
    except sqlite3.Error:
        return False


def _backup_schema_27_before_upgrade(conn: sqlite3.Connection, path: Path) -> None:
    """Create one WAL-consistent rollback copy before the schema-28 upgrade."""
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if not has_meta:
        return
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if not row or str(row["value"] or "") != "27":
        return
    backup_path = path.with_name(f"{path.name}.schema27-backup")
    if backup_path.exists() or backup_path.is_symlink():
        if backup_path.is_symlink() or not backup_path.is_file():
            raise RuntimeError("schema_27_backup_invalid")
        if not _valid_schema_27_backup_content(backup_path):
            raise RuntimeError("schema_27_backup_invalid")
        try:
            backup_path.chmod(0o600)
        except OSError as exc:
            raise RuntimeError("schema_27_backup_permissions_failed") from exc
        if not _valid_schema_27_backup(backup_path):
            raise RuntimeError("schema_27_backup_invalid")
        return
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.schema27-backup-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
        backup_conn = sqlite3.connect(temporary_name)
        try:
            conn.backup(backup_conn)
        finally:
            backup_conn.close()
        temporary_path = Path(temporary_name)
        temporary_path.chmod(0o600)
        temporary_path.replace(backup_path)
        backup_path.chmod(0o600)
        if not _valid_schema_27_backup(backup_path):
            raise RuntimeError("schema_27_backup_invalid")
    except Exception as exc:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise RuntimeError("schema_27_backup_failed") from exc


def _valid_schema_27_backup(path: Path) -> bool:
    try:
        if path.stat().st_mode & 0o777 != 0o600:
            return False
    except OSError:
        return False
    return _valid_schema_27_backup_content(path)


def _valid_schema_27_backup_content(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        backup = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            integrity = backup.execute("PRAGMA integrity_check").fetchone()
            version = backup.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            return bool(
                integrity
                and str(integrity[0] or "").lower() == "ok"
                and version
                and str(version[0] or "") == "27"
            )
        finally:
            backup.close()
    except sqlite3.Error:
        return False


def harden_sqlite_permissions(db_path: str | Path) -> None:
    path = Path(db_path)
    _chmod_private_dir(path.parent)
    for candidate in _sqlite_files(path):
        if candidate.exists():
            candidate.chmod(0o600)


def _sqlite_files(path: Path) -> tuple[Path, Path, Path]:
    return (path, Path(f"{path}-wal"), Path(f"{path}-shm"))


def _chmod_private_dir(path: Path) -> None:
    if str(path) in {"", "."}:
        return
    path.chmod(0o700)


def _migrate_frame_signal_raw_default(conn: sqlite3.Connection) -> None:
    columns = conn.execute("PRAGMA table_info(frame_signals)").fetchall()
    raw_column = next((column for column in columns if column["name"] == "raw_ref_deleted"), None)
    if raw_column is None or str(raw_column["dflt_value"]).strip("'\"") == "0":
        return

    _execute_sql_script(
        conn,
        """
        ALTER TABLE frame_signals RENAME TO frame_signals_old;

        CREATE TABLE frame_signals (
          id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          segment_id TEXT,
          captured_at TEXT NOT NULL,
          display_id TEXT,
          app_name TEXT,
          bundle_id TEXT,
          window_title_redacted TEXT,
          url_redacted TEXT,
          domain TEXT,
          ocr_redacted_summary TEXT,
          safe_keywords TEXT,
          privacy_flags TEXT,
          redaction_status TEXT DEFAULT 'redacted',
          raw_ref_deleted INTEGER DEFAULT 0,
          deletion_group_id TEXT NOT NULL
        );

        INSERT INTO frame_signals(
          id, session_id, segment_id, captured_at, display_id, app_name, bundle_id,
          window_title_redacted, url_redacted, domain, ocr_redacted_summary,
          safe_keywords, privacy_flags, redaction_status, raw_ref_deleted, deletion_group_id
        )
        SELECT
          id, session_id, segment_id, captured_at, display_id, app_name, bundle_id,
          window_title_redacted, url_redacted, domain, ocr_redacted_summary,
          safe_keywords, privacy_flags, redaction_status, raw_ref_deleted, deletion_group_id
        FROM frame_signals_old;

        DROP TABLE frame_signals_old;
        CREATE INDEX IF NOT EXISTS idx_frame_signals_time ON frame_signals(captured_at);
        """
    )


def _migrate_activity_confirmation_columns(conn: sqlite3.Connection) -> None:
    schema_row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    try:
        pre_migration_schema = int(schema_row["value"]) if schema_row else SCHEMA_VERSION
    except (TypeError, ValueError):
        pre_migration_schema = SCHEMA_VERSION
    # The schema version is the durable migration marker.  Column existence is
    # not sufficient: a killed prior attempt may have completed one or more
    # ALTER TABLE statements while meta still says 27.  In that state every
    # ALTER and every legacy-row backfill below must be safely repeatable.
    migrating_to_v28 = pre_migration_schema < 28
    item_columns = {
        column["name"]
        for column in conn.execute("PRAGMA table_info(activity_confirmation_items)").fetchall()
    }
    item_column_definitions = {
        "group_key": "TEXT NOT NULL DEFAULT ''",
        "group_signature": "TEXT NOT NULL DEFAULT ''",
        "subject": "TEXT NOT NULL DEFAULT '{}'",
        "evidence": "TEXT NOT NULL DEFAULT ''",
        "cleaned_content": "TEXT NOT NULL DEFAULT ''",
        "clarification": "TEXT NOT NULL DEFAULT '{}'",
        "clarification_note": "TEXT NOT NULL DEFAULT ''",
        "question_kind": "TEXT NOT NULL DEFAULT 'occurrence'",
        "candidate_origin": "TEXT NOT NULL DEFAULT 'explicit_claim'",
        "priority": "TEXT NOT NULL DEFAULT 'normal'",
        "episode_id": "TEXT NOT NULL DEFAULT ''",
        "safe_signal_types": "TEXT NOT NULL DEFAULT '[]'",
        "suggested_workstream": "TEXT NOT NULL DEFAULT ''",
        "suggested_outcome": "TEXT NOT NULL DEFAULT 'unknown'",
        "user_workstream": "TEXT NOT NULL DEFAULT ''",
        "user_outcome": "TEXT NOT NULL DEFAULT ''",
        "notification_eligible": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in item_column_definitions.items():
        if name not in item_columns:
            conn.execute(
                f"ALTER TABLE activity_confirmation_items ADD COLUMN {name} {definition}"
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_confirmation_group_key "
        "ON activity_confirmation_items(group_key, observed_start)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_confirmation_group_signature "
        "ON activity_confirmation_items(group_signature, observed_start)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_confirmation_episode "
        "ON activity_confirmation_items(episode_id, observed_start)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_confirmation_origin_state "
        "ON activity_confirmation_items(candidate_origin, state, observed_start)"
    )
    if migrating_to_v28:
        conn.execute(
            "UPDATE activity_confirmation_items SET priority=CASE "
            "WHEN event_type IN ('interview','meeting','application','submission','send','call','purchase','publish') "
            "THEN 'high' ELSE 'normal' END,"
            "safe_signal_types='[\"legacy_explicit_claim\"]',"
            "notification_eligible=1"
        )
    else:
        conn.execute(
            "UPDATE activity_confirmation_items SET question_kind='occurrence' "
            "WHERE question_kind NOT IN ('occurrence','business_context','outcome')"
        )
        conn.execute(
            "UPDATE activity_confirmation_items SET candidate_origin='explicit_claim' "
            "WHERE candidate_origin NOT IN ('explicit_claim','business_episode','manual')"
        )
        conn.execute(
            "UPDATE activity_confirmation_items SET priority='normal' "
            "WHERE priority NOT IN ('high','normal','low')"
        )
    legacy_outcomes = conn.execute(
        "SELECT 1 FROM activity_confirmation_items "
        "WHERE suggested_outcome IN ('progressed','preparation') "
        "OR user_outcome IN ('progressed','preparation') LIMIT 1"
    ).fetchone()
    if legacy_outcomes:
        conn.execute(
            "UPDATE activity_confirmation_items SET suggested_outcome=CASE "
            "WHEN suggested_outcome='progressed' THEN 'progressing' "
            "WHEN suggested_outcome='preparation' THEN 'preparation_only' "
            "ELSE suggested_outcome END, user_outcome=CASE "
            "WHEN user_outcome='progressed' THEN 'progressing' "
            "WHEN user_outcome='preparation' THEN 'preparation_only' "
            "ELSE user_outcome END"
        )
    invalid_outcomes = conn.execute(
        "SELECT 1 FROM activity_confirmation_items WHERE "
        "suggested_outcome NOT IN ('completed','progressing','blocked','preparation_only','unknown') "
        "OR (user_outcome<>'' AND user_outcome NOT IN "
        "('completed','progressing','blocked','preparation_only','unknown')) LIMIT 1"
    ).fetchone()
    if invalid_outcomes:
        conn.execute(
            "UPDATE activity_confirmation_items SET suggested_outcome=CASE "
            "WHEN suggested_outcome IN ('completed','progressing','blocked','preparation_only','unknown') "
            "THEN suggested_outcome ELSE 'unknown' END, user_outcome=CASE "
            "WHEN user_outcome='' OR user_outcome IN "
            "('completed','progressing','blocked','preparation_only','unknown') "
            "THEN user_outcome ELSE 'unknown' END"
        )

    columns = {
        column["name"]
        for column in conn.execute("PRAGMA table_info(activity_confirmation_calibrations)").fetchall()
    }
    if "reason_counts" not in columns:
        conn.execute(
            "ALTER TABLE activity_confirmation_calibrations "
            "ADD COLUMN reason_counts TEXT NOT NULL DEFAULT '{}'"
        )
    # Do not take a write lock on every ensure_ready() call. Document-event
    # commits and authorization revocation deliberately serialize on SQLite;
    # an unconditional no-op UPDATE here would make a second connection wait
    # before it can even announce the revocation. Only legacy pre-release v25
    # rows need normalization.
    trust_degradation_changed = False
    legacy_state = conn.execute(
        "SELECT 1 FROM activity_confirmation_items "
        "WHERE state IN ('evidence_confirmed','evidence_rejected') LIMIT 1"
    ).fetchone()
    if legacy_state:
        # The historical state name is not itself evidence.  Rows with no
        # owner answer are screen-derived clues and return to the review queue;
        # rows with an explicit answer keep every answer field and are mapped
        # only to the state that the existing answer already implies.
        changed = conn.execute(
            "UPDATE activity_confirmation_items SET state=CASE answer "
            "WHEN 'occurred' THEN 'confirmed' "
            "WHEN 'not_occurred' THEN 'rejected' "
            "WHEN 'misrecognized' THEN 'rejected' "
            "WHEN 'ignored' THEN 'ignored' "
            "WHEN 'defer' THEN 'deferred' "
            "ELSE 'invalidated' END,notification_eligible=0 "
            "WHERE state='evidence_confirmed' AND answer IS NOT NULL"
        ).rowcount
        changed += conn.execute(
            "UPDATE activity_confirmation_items SET state='pending',notification_eligible=0 "
            "WHERE state='evidence_confirmed' AND answer IS NULL"
        ).rowcount
        changed += conn.execute(
            "UPDATE activity_confirmation_items SET state='rejected',notification_eligible=0 "
            "WHERE state='evidence_rejected'"
        ).rowcount
        trust_degradation_changed = trust_degradation_changed or changed > 0

    # Schema 30+ never treats a bare state label as an owner confirmation. This
    # includes malformed manual-origin rows: a valid manual confirmation always
    # carries answer='occurred', so the explicit owner-answer path below remains
    # untouched while NULL answers fail closed.
    unsafe_confirmed = conn.execute(
        "SELECT 1 FROM activity_confirmation_items "
        "WHERE state='confirmed' AND answer IS NULL LIMIT 1"
    ).fetchone()
    if unsafe_confirmed:
        changed = conn.execute(
            "UPDATE activity_confirmation_items SET state='invalidated',"
            "notification_eligible=0,answered_at=NULL "
            "WHERE state='confirmed' AND answer IS NULL"
        ).rowcount
        trust_degradation_changed = trust_degradation_changed or changed > 0

    if trust_degradation_changed:
        # Settlement revisions, package caches and sampled/diagnostic counters
        # may all have incorporated the invalid trust claim.  Invalidate them
        # in the same migration transaction so no mixed revision is observable.
        invalidated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute("DELETE FROM activity_confirmation_settlements")
        conn.execute(
            "UPDATE distribution_packages SET cache_invalidated_at=? "
            "WHERE cache_ref IS NOT NULL AND cache_ref<>'' AND cache_invalidated_at IS NULL",
            (invalidated_at,),
        )
        conn.execute(
            "DELETE FROM settings WHERE key LIKE 'activity_confirmation_sampled_out:%' "
            "OR key LIKE 'activity_confirmation_episode_diagnostics:%'"
        )

    # The schema-26 grouping migration reorganizes unresolved source-bound rows
    # only. Historical answers and content-free calibration counters are
    # deliberately left byte-for-byte untouched.
    needs_group_backfill = conn.execute(
        "SELECT 1 FROM activity_confirmation_items "
        "WHERE state IN ('pending','deferred') "
        "AND (group_key='' OR group_signature='' OR subject='{}' OR evidence='') LIMIT 1"
    ).fetchone()
    if needs_group_backfill:
        from .activity_confirmations import backfill_unresolved_confirmation_groups

        backfill_unresolved_confirmation_groups(conn)

    # Policy-only queue cleanup is intentionally not run as an implicit schema
    # migration.  v0.3.20 exposes an explicit dry-run/apply command so an
    # operator can take a WAL-consistent backup and inspect counts before any
    # unresolved row is regrouped or filtered.
    policy_marker = conn.execute(
        "SELECT value FROM settings WHERE key='activity_confirmation_policy_version'"
    ).fetchone()
    unresolved_confirmation = conn.execute(
        "SELECT 1 FROM activity_confirmation_items "
        "WHERE state IN ('pending','deferred') AND answer IS NULL LIMIT 1"
    ).fetchone()
    if not policy_marker and not unresolved_confirmation:
        from .activity_confirmations import CONFIRMATION_POLICY_VERSION

        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)",
            (
                "activity_confirmation_policy_version",
                str(CONFIRMATION_POLICY_VERSION),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )


def _migrate_frame_signal_region_columns(conn: sqlite3.Connection) -> None:
    columns = {column["name"] for column in conn.execute("PRAGMA table_info(frame_signals)").fetchall()}
    if "screen_region_signal" not in columns:
        conn.execute("ALTER TABLE frame_signals ADD COLUMN screen_region_signal TEXT")


def _backfill_frame_signal_regions(conn: sqlite3.Connection) -> None:
    try:
        from .screen_regions import classify_screen_region
    except ImportError:
        return
    rows = conn.execute(
        """
        SELECT id, app_name, window_title_redacted, ocr_redacted_summary, privacy_flags
        FROM frame_signals
        WHERE COALESCE(screen_region_signal, '') = ''
        LIMIT 5000
        """
    ).fetchall()
    for row in rows:
        try:
            privacy_flags = json.loads(row["privacy_flags"] or "[]")
        except json.JSONDecodeError:
            privacy_flags = []
        signal = classify_screen_region(
            {
                "app_name": row["app_name"] or "",
                "window_title": row["window_title_redacted"] or "",
                "ocr_text": row["ocr_redacted_summary"] or "",
                "privacy_flags": privacy_flags,
            }
        )
        conn.execute(
            "UPDATE frame_signals SET screen_region_signal = ? WHERE id = ?",
            (json.dumps(signal, ensure_ascii=False, sort_keys=True), row["id"]),
        )


def _migrate_source_status_values(conn: sqlite3.Connection) -> None:
    _migrate_source_status_table(conn, "ha_source_records")
    _migrate_source_status_table(conn, "source_revocation_events")


def _migrate_source_status_table(conn: sqlite3.Connection, table_name: str) -> None:
    placeholders = ", ".join("?" for _ in SOURCE_STATUSES)
    needs_update = conn.execute(
        f"SELECT 1 FROM {table_name} WHERE source_status NOT IN ({placeholders}) LIMIT 1",
        SOURCE_STATUSES,
    ).fetchone()
    if not needs_update:
        return
    conn.execute(
        f"UPDATE {table_name} SET source_status = 'revoked' WHERE source_status NOT IN ({placeholders})",
        SOURCE_STATUSES,
    )


def _set_schema_version(conn: sqlite3.Connection) -> None:
    version = str(SCHEMA_VERSION)
    current = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if current and current["value"] == version:
        return
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (version,),
    )


def _refuse_schema_downgrade(conn: sqlite3.Connection) -> None:
    """Never let an older core silently relabel a newer database."""
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if not has_meta:
        return
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if not row:
        return
    try:
        current = int(row["value"])
    except (TypeError, ValueError):
        raise sqlite3.DatabaseError("invalid PAM schema_version")
    if current > SCHEMA_VERSION:
        raise sqlite3.DatabaseError(
            f"PAM database schema {current} is newer than supported schema {SCHEMA_VERSION}; refusing downgrade"
        )


def _migrate_capture_health_columns(conn: sqlite3.Connection) -> None:
    columns = {column["name"] for column in conn.execute("PRAGMA table_info(capture_coverage_ledger)").fetchall()}
    additions = {
        "partial_redacted_count": "INTEGER DEFAULT 0",
        "duplicate_exact_count": "INTEGER DEFAULT 0",
        "partial_duplicate_count": "INTEGER DEFAULT 0",
        "vlm_review_count": "INTEGER DEFAULT 0",
        "vlm_cache_hit_count": "INTEGER DEFAULT 0",
        "vlm_clear_count": "INTEGER DEFAULT 0",
        "vlm_partial_redacted_count": "INTEGER DEFAULT 0",
        "vlm_blocked_count": "INTEGER DEFAULT 0",
        "vlm_review_required_count": "INTEGER DEFAULT 0",
        "vlm_timeout_count": "INTEGER DEFAULT 0",
        "vlm_unavailable_count": "INTEGER DEFAULT 0",
        "vlm_invalid_count": "INTEGER DEFAULT 0",
        "vlm_disabled_count": "INTEGER DEFAULT 0",
        "vlm_last_status": "TEXT",
        "vlm_model": "TEXT",
        "vlm_revision_key": "TEXT",
        "vlm_canary_status": "TEXT",
        "vlm_elapsed_ms": "INTEGER DEFAULT 0",
        "vlm_canary_elapsed_ms": "INTEGER DEFAULT 0",
        "vlm_image_downscaled": "INTEGER DEFAULT 0",
        "vlm_image_original_bytes": "INTEGER DEFAULT 0",
        "vlm_image_submitted_bytes": "INTEGER DEFAULT 0",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE capture_coverage_ledger ADD COLUMN {name} {definition}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_capture_coverage_day_display "
        "ON capture_coverage_ledger(datetime(started_at), display_id)"
    )


def _migrate_vlm_pending_columns(conn: sqlite3.Connection) -> None:
    columns = {column["name"] for column in conn.execute("PRAGMA table_info(vlm_pending_jobs)").fetchall()}
    additions = {
        "next_retry_at": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE vlm_pending_jobs ADD COLUMN {name} {definition}")


def _migrate_dedupe_columns(conn: sqlite3.Connection) -> None:
    frame_columns = {column["name"] for column in conn.execute("PRAGMA table_info(frame_signals)").fetchall()}
    frame_additions = {
        "dedupe_status": "TEXT DEFAULT 'unique'",
        "dedupe_note": "TEXT",
        "dedupe_duplicate_lines": "INTEGER DEFAULT 0",
        "dedupe_retained_lines": "INTEGER DEFAULT 0",
        "dedupe_content_hash": "TEXT",
        "dedupe_line_hashes": "TEXT",
    }
    for name, definition in frame_additions.items():
        if name not in frame_columns:
            conn.execute(f"ALTER TABLE frame_signals ADD COLUMN {name} {definition}")

    activity_columns = {column["name"] for column in conn.execute("PRAGMA table_info(extracted_activities)").fetchall()}
    activity_additions = {
        "dedupe_status": "TEXT DEFAULT 'unique'",
        "dedupe_note": "TEXT",
        "dedupe_duplicate_lines": "INTEGER DEFAULT 0",
        "dedupe_retained_lines": "INTEGER DEFAULT 0",
        "dedupe_content_hash": "TEXT",
    }
    for name, definition in activity_additions.items():
        if name not in activity_columns:
            conn.execute(f"ALTER TABLE extracted_activities ADD COLUMN {name} {definition}")


def _migrate_document_signal_columns(conn: sqlite3.Connection) -> None:
    columns = {column["name"] for column in conn.execute("PRAGMA table_info(document_signals)").fetchall()}
    additions = {
        "semantic_match": "INTEGER DEFAULT 1",
        "match_status": "TEXT DEFAULT 'matched_text'",
        "document_source_category": "TEXT",
        "document_source_scope": "TEXT",
        "document_subject_scope": "TEXT",
        "document_memory_policy": "TEXT",
        "document_classification_confidence": "REAL DEFAULT 0.0",
        "document_classification_reasons": "TEXT",
        "document_risk_flags": "TEXT",
        "document_parse_status": "TEXT",
        "document_parse_method": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE document_signals ADD COLUMN {name} {definition}")


def _migrate_file_watch_checkpoint_columns(conn: sqlite3.Connection) -> None:
    columns = {
        column["name"]
        for column in conn.execute("PRAGMA table_info(file_watch_checkpoints)").fetchall()
    }
    if "coverage_started_at" not in columns:
        conn.execute("ALTER TABLE file_watch_checkpoints ADD COLUMN coverage_started_at TEXT")
    if "benign_event_count" not in columns:
        conn.execute(
            "ALTER TABLE file_watch_checkpoints ADD COLUMN benign_event_count INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_file_change_attribution_schema(conn: sqlite3.Connection) -> None:
    """Upgrade the aggregate-only file history without inventing actors.

    One synthetic version row represents each legacy daily aggregate.  Its
    ``observed_version_count`` preserves the aggregate's multiplicity, while
    ``backfill_unknown`` makes it impossible for a later query to claim that
    every historical write was made by AI or by the owner.
    """

    state_columns = {
        column["name"]
        for column in conn.execute("PRAGMA table_info(file_watch_state)").fetchall()
    }
    if "project_label" not in state_columns:
        conn.execute(
            "ALTER TABLE file_watch_state ADD COLUMN project_label TEXT NOT NULL DEFAULT ''"
        )
    if "document_path_hmac" not in state_columns:
        conn.execute(
            "ALTER TABLE file_watch_state ADD COLUMN document_path_hmac TEXT NOT NULL DEFAULT ''"
        )

    version_columns = {
        column["name"]
        for column in conn.execute("PRAGMA table_info(file_change_versions)").fetchall()
    }
    if "document_path_hmac" not in version_columns:
        conn.execute(
            "ALTER TABLE file_change_versions ADD COLUMN document_path_hmac TEXT NOT NULL DEFAULT ''"
        )

    # The display exclusion follows immutable lineage for a particular root.
    # A covering index avoids scanning every retained version on each refresh.
    # Add it after the legacy column migration; no history is rewritten.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_change_versions_root_document "
        "ON file_change_versions(root_id,document_path_hmac)"
    )

    conn.execute(
        "INSERT OR IGNORE INTO file_change_versions("
        "id,aggregate_event_id,file_id,root_id,project_label,document_path_hmac,file_name_redacted,file_type,change_kind,"
        "changed_at,content_modified_at,detail_level,actor_source,actor_tool,actor_confidence,"
        "actor_reason_codes,coverage_state,helper_event_id,observed_version_count,deletion_group_id,created_at,updated_at"
        ") SELECT "
        "'fcv_backfill_' || id,id,file_id,root_id,project_label,'',file_name_redacted,file_type,change_kind,"
        "last_changed_at,NULL,detail_level,'unattributed','','unknown','[\"historical_aggregate_only\"]',"
        "'backfill_unknown','',MAX(1,version_count),deletion_group_id,created_at,updated_at "
        "FROM file_change_events AS events "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM file_change_versions AS versions "
        "WHERE versions.aggregate_event_id=events.id"
        ")"
    )


def _migrate_ha_automation_columns(conn: sqlite3.Connection) -> None:
    columns = {column["name"] for column in conn.execute("PRAGMA table_info(ha_automation_jobs)").fetchall()}
    additions = {
        "processing_started_at": "TEXT",
        "next_retry_at": "TEXT",
        "dead_letter_reason": "TEXT",
        "hermes_result_status": "TEXT",
        "memories_written_count": "INTEGER DEFAULT 0",
        "skipped_reason": "TEXT",
        "hermes_result_json": "TEXT",
        "written_candidate_count": "INTEGER DEFAULT 0",
        "skipped_candidate_count": "INTEGER DEFAULT 0",
        "written_candidate_types": "TEXT",
        "skipped_reasons": "TEXT",
        "write_audit_summary": "TEXT",
        "written_memory_summaries": "TEXT",
        "memory_report_status": "TEXT",
        "memory_report_target": "TEXT",
        "memory_report_error": "TEXT",
        "memory_reported_at": "TEXT",
        "memory_report_attempt_count": "INTEGER DEFAULT 0",
        "memory_report_next_retry_at": "TEXT",
        "candidate_count": "INTEGER DEFAULT 0",
        "candidate_skip_reason": "TEXT",
        "eligible_candidate_count": "INTEGER DEFAULT 0",
        "review_only_candidate_count": "INTEGER DEFAULT 0",
        "ephemeral_candidate_count": "INTEGER DEFAULT 0",
        "confirmed_action_count": "INTEGER DEFAULT 0",
        "context_clue_count": "INTEGER DEFAULT 0",
        "noise_or_reference_count": "INTEGER DEFAULT 0",
        "single_evidence_eligible_count": "INTEGER DEFAULT 0",
        "subject_risk_candidate_count": "INTEGER DEFAULT 0",
        "single_evidence_demoted_count": "INTEGER DEFAULT 0",
        "media_or_file_listing_ephemeral_count": "INTEGER DEFAULT 0",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE ha_automation_jobs ADD COLUMN {name} {definition}")


def _migrate_quality_feedback_columns(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS quality_feedback (
          id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          target_type TEXT NOT NULL,
          target_id TEXT NOT NULL,
          target_started_at TEXT,
          target_ended_at TEXT,
          issue_type TEXT NOT NULL,
          expected_status TEXT NOT NULL,
          note_redacted TEXT,
          deletion_group_id TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quality_feedback_time ON quality_feedback(target_started_at, target_ended_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quality_feedback_target ON quality_feedback(target_type, target_id)")


def _migrate_quality_advice_columns(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS quality_advice_runs (
          id TEXT PRIMARY KEY,
          run_key TEXT NOT NULL UNIQUE,
          started_at TEXT NOT NULL,
          ended_at TEXT NOT NULL,
          profile TEXT NOT NULL,
          status TEXT NOT NULL,
          package_id TEXT,
          memory_candidate_count INTEGER DEFAULT 0,
          document_candidate_count INTEGER DEFAULT 0,
          document_match_count INTEGER DEFAULT 0,
          issue_count INTEGER DEFAULT 0,
          recommendation_count INTEGER DEFAULT 0,
          summary_redacted TEXT,
          advice_json TEXT,
          last_error TEXT,
          created_at TEXT NOT NULL,
          completed_at TEXT,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quality_advice_runs_status ON quality_advice_runs(status, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quality_advice_runs_time ON quality_advice_runs(started_at, ended_at)")


def _migrate_user_decision_columns(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_decisions (
          item_key TEXT PRIMARY KEY,
          item_type TEXT NOT NULL,
          source_kind TEXT NOT NULL,
          action TEXT NOT NULL,
          safe_summary TEXT NOT NULL,
          user_note TEXT,
          decision_signature TEXT NOT NULL,
          generalize_scope TEXT NOT NULL,
          applied_plan_id TEXT,
          source_package_id TEXT,
          source_activity_ids TEXT NOT NULL DEFAULT '[]',
          source_start TEXT,
          source_end TEXT,
          deletion_group_ids TEXT NOT NULL DEFAULT '[]',
          provenance_status TEXT NOT NULL DEFAULT 'legacy_unknown',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(user_decisions)").fetchall()}
    additions = {
        "source_package_id": "TEXT",
        "source_activity_ids": "TEXT NOT NULL DEFAULT '[]'",
        "source_start": "TEXT",
        "source_end": "TEXT",
        "deletion_group_ids": "TEXT NOT NULL DEFAULT '[]'",
        "provenance_status": "TEXT NOT NULL DEFAULT 'legacy_unknown'",
    }
    for name, declaration in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE user_decisions ADD COLUMN {name} {declaration}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_decisions_action ON user_decisions(action, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_decisions_signature ON user_decisions(decision_signature, generalize_scope)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_decisions_source_package ON user_decisions(source_package_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_decisions_source_time ON user_decisions(source_start, source_end)")
