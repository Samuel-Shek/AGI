from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ai_handoff import (
    build_ai_handoff_package,
    ai_handoff_status,
    render_ai_handoff_markdown,
    write_ai_handoff_export,
)
from .ai_connector import (
    DEFAULT_AI_CONNECTOR_HOST,
    DEFAULT_AI_CONNECTOR_PORT,
    ai_connector_status,
    build_ai_connector_package,
    serve_ai_connector,
    start_ai_connector,
    stop_ai_connector,
)
from .activity_sessions import (
    get_current_activity_session,
    list_activity_sessions,
    render_activity_sessions_markdown,
    show_activity_session,
)
from .activity_confirmations import (
    REASONS as ACTIVITY_CONFIRMATION_REASONS,
    activity_confirmation_revision,
    answer_activity_confirmation,
    answer_activity_confirmation_group,
    clarify_activity_confirmation_group,
    configure_activity_confirmations,
    confirmed_activity_view,
    list_activity_confirmation_cards,
    list_activity_confirmations,
    manual_add_confirmed_activity,
    revoke_confirmation_calibration,
    revalidate_unresolved_confirmation_policy,
    settle_activity_confirmations,
)
from .browser_signals import install_native_host_manifests, native_host_core_root, native_host_loop
from .daemon import (
    cleanup_retention,
    cleanup_retention_preview,
    configure_screen_capture,
    configure_hygiene_cleanup,
    configure_privacy_profile,
    get_quick_status,
    get_status,
    install_launch_agent,
    pause,
    resume,
    run_daemon,
    uninstall_launch_agent,
)
from .data_hygiene import (
    DEFAULT_KEEP_BACKUPS,
    DEFAULT_MIN_AGE_DAYS,
    DEFAULT_QUARANTINE_DAYS,
    HYGIENE_CLEANUP_CONFIRMATION,
    backup_hygiene_report,
    data_hygiene_report,
    hygiene_cleanup_report,
)
from .ha_automation import configure_ha_automation, ha_automation_status, run_ha_automation_once, validate_ha_automation
from .context_console import (
    ContractError,
    CONTEXT_FEATURE_CONFIRMATION,
    CONTEXT_SNAPSHOT_VERSION,
    DELIVERY_STATUS_VERSION,
    LOCAL_HANDOFF_VERSION,
    MAX_CONTRACT_BYTES,
    MEMORY_CANDIDATE_LIST_VERSION,
    PRODUCT_EXPERIMENT_FINALIZE_CONFIRMATION,
    PRODUCT_EXPERIMENT_STATUS_VERSION,
    build_local_handoff,
    build_context_bundle,
    configure_context_console,
    configure_context_local_timezone,
    context_snapshot,
    context_console_status_path,
    create_memory_candidate,
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
    record_component_baseline,
    record_product_feedback,
    register_context_label,
    register_context_principal,
    runtime_stability_status,
    start_product_experiment,
    _validate_search_query,
)
from .daily_review import daily_review
from .document_changes import (
    configure_document_changes,
    document_authorization_lock,
    document_changes_status,
    list_document_changes,
    resolve_document_change_range,
)
from .diagnostics import diagnostics_status, render_diagnostics_markdown
from .decision_queue import (
    apply_decision_queue_plan,
    decision_queue_list,
    preview_decision_queue_write,
    record_decision_queue_action,
)
from .ha_source import (
    expire_packages,
    generate_package,
    get_package,
    get_package_handoff_summary,
    get_package_memory_candidates,
    list_packages,
    revoke_package,
)
from .identity_collision import (
    apply_document_signal_identity_collisions,
    preview_document_signal_identity_collisions,
    reverse_document_signal_identity_collisions,
)
from .lifecycle import delete_range
from .memory_audit import memory_audit, render_memory_audit_markdown
from .memory_approval import (
    apply_memory_approval,
    memory_approval_candidates,
    memory_approval_records,
    memory_approval_status,
    preview_memory_approval,
)
from .memory_ledger import memory_ledger_status, refresh_memory_ledger, sync_memory_ledger
from .memory_nominations import nominate as nominate_memory
from .memory_review import ACTIONS as MEMORY_REVIEW_ACTIONS
from .memory_review import (
    MemoryReviewError,
    apply_review_batch,
    memory_review_candidates,
    memory_review_status,
    preview_review_batch,
    read_memory_text,
)
from .pipeline import ingest_events, iso, load_jsonl, parse_time, search, timeline
from .privacy_policy import privacy_policy_preview, privacy_policy_status, privacy_profile_preview
from .quality import (
    add_quality_feedback,
    list_quality_feedback,
    quality_report,
    render_quality_markdown,
)
from .quality_advice import (
    configure_quality_advice,
    quality_advice_status,
    run_quality_advice_once,
    validate_quality_advice,
)
from .report_context import report_context
from .reclassify import reclassify_existing
from .server import serve
from .storage import SQLITE_BUSY_TIMEOUT_MS, connect_readonly, ensure_ready, init_schema
from .stability import (
    REQUIRED_DRILLS,
    build_soak_report,
    record_resource_baseline,
    record_soak_evidence_event,
    refresh_daily_snapshot,
    render_soak_report_markdown,
)
from .swift_helpers import ensure_all_helpers
from .timeline_export import (
    cleaned_timeline_export,
    render_cleaned_timeline_markdown,
    render_learning_timeline_markdown,
    write_timeline_markdown,
)


DEFAULT_DB = "data/activity.sqlite"
DEFAULT_PACKAGE_DIR = "data/ha_packages"


def main() -> None:
    parser = argparse.ArgumentParser(description="AGI local-first personal context recall core")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize the local database")
    add_db(init)

    ingest_sample = sub.add_parser("ingest-sample", help="Ingest bundled sample events")
    add_db(ingest_sample)
    ingest_sample.add_argument("--file", default="samples/sample_events.jsonl")

    ingest_jsonl = sub.add_parser("ingest-jsonl", help="Ingest JSONL activity events")
    add_db(ingest_jsonl)
    ingest_jsonl.add_argument("file")

    tl = sub.add_parser("timeline", help="List clear extracted activities")
    add_db(tl)
    tl.add_argument("--from", dest="start")
    tl.add_argument("--to", dest="end")
    tl.add_argument("--format", choices=("json", "markdown", "learning-markdown"), default="json")
    tl.add_argument("--out", help="Write the timeline output to a local file")

    se = sub.add_parser("search", help="Search clear extracted activities")
    add_db(se)
    se.add_argument(
        "--query-stdin",
        action="store_true",
        help="Read bounded JSON {query} from stdin so search text never enters argv",
    )
    se.add_argument("--app")
    se.add_argument("--from", dest="start")
    se.add_argument("--to", dest="end")

    review = sub.add_parser("daily-review", help="Generate a local review from clear activity only")
    add_db(review)
    review.add_argument("--from", dest="start")
    review.add_argument("--to", dest="end")

    report_context_parser = sub.add_parser(
        "report-context",
        help="Print the fixed, confirmed-only daily report context contract",
    )
    add_db(report_context_parser)
    report_context_parser.add_argument("--date", dest="report_date")
    report_context_parser.add_argument("--format", choices=("json",), default="json")

    context_console = sub.add_parser(
        "context-console",
        help="Manage the feature-flagged trustworthy context vertical slice",
    )
    context_console_sub = context_console.add_subparsers(dest="context_console_command", required=True)
    context_status = context_console_sub.add_parser("status", help="Read fail-closed context status")
    add_db(context_status)
    context_configure = context_console_sub.add_parser("configure", help="Explicitly enable or disable the source candidate")
    add_db(context_configure)
    context_configure.add_argument("--enabled", required=True, choices=("on", "off"))
    context_configure.add_argument("--confirm", default="")
    context_timezone = context_console_sub.add_parser(
        "configure-timezone",
        help="Configure the explicit IANA timezone for local-natural-day policies",
    )
    add_db(context_timezone)
    context_timezone.add_argument("--timezone", required=True)
    context_label = context_console_sub.add_parser("register-label", help="Register one private approved label")
    add_db(context_label)
    context_label.add_argument("--kind", required=True, choices=("project", "workstream", "object", "version", "subject", "target"))
    context_label.add_argument("--ref", required=True)
    context_label.add_argument("--label", required=True)
    context_label.add_argument("--project-ref", default="")
    context_label.add_argument("--subject-scope", default="")
    context_label.add_argument("--target-scope", choices=("local", "external"))
    context_label.add_argument(
        "--allowed-privacy-class",
        dest="allowed_privacy_classes",
        action="append",
        choices=("private_local", "sensitive_local", "restricted_local"),
        help="Repeat for each explicitly allowed target privacy class",
    )
    context_label.add_argument("--export-policy", choices=("local_only", "explicit_export"))
    context_principal = context_console_sub.add_parser("register-principal", help="Register a verifier fingerprint; key comes from a private environment variable")
    add_db(context_principal)
    context_principal.add_argument("--principal-id", required=True)
    context_principal.add_argument("--key-id", required=True)
    context_principal.add_argument("--principal-kind", required=True, choices=("ai_lifecycle", "user_confirmation", "file_adapter", "git_adapter", "external_adapter", "bundle_consumer", "delivery_provider"))
    context_principal.add_argument("--project-ref", default="")
    context_principal.add_argument("--target-ref", default="")
    context_principal.add_argument("--target-scope", choices=("local", "external"), default="")
    context_principal.add_argument("--secret-env", default="PAM_CONTEXT_RECEIPT_KEY")
    context_ingest = context_console_sub.add_parser("ingest-receipt", help="Read ContextReceiptV1 JSON only from stdin")
    add_db(context_ingest)
    context_ingest.add_argument("--secret-env", default="PAM_CONTEXT_RECEIPT_KEY")
    context_evidence = context_console_sub.add_parser("ingest-evidence", help="Read ControlledEvidenceV1 JSON only from stdin")
    add_db(context_evidence)
    context_evidence.add_argument("--secret-env", default="PAM_CONTEXT_RECEIPT_KEY")
    context_bundle = context_console_sub.add_parser("bundle", help="Read ContextBundleV1 with stable local filters")
    add_db(context_bundle)
    context_bundle.add_argument("--project-ref", default="")
    context_bundle.add_argument("--from", dest="start_at", default="")
    context_bundle.add_argument("--to", dest="end_at", default="")
    context_bundle.add_argument(
        "--query-stdin",
        action="store_true",
        help="Read bounded JSON {query} from stdin; query text is forbidden in argv",
    )
    context_bundle.add_argument("--include-clues", action="store_true")
    context_bundle.add_argument("--limit", type=int, default=100)
    for command_name, help_text in (
        ("snapshot", "Read ContextSnapshotV1 from bounded JSON stdin"),
        ("memory-candidates", "Read MemoryCandidateListV1 from bounded JSON stdin"),
        ("delivery-status", "Read DeliveryStatusV1 from bounded JSON stdin"),
        ("local-handoff", "Create one verified-only LocalHandoffV1 from bounded JSON stdin"),
        ("owner-action", "Apply one explicit OwnerContextActionV1 from bounded JSON stdin"),
        ("product-feedback", "Record one source-bound ProductFeedbackV1 from bounded JSON stdin"),
    ):
        phase2_command = context_console_sub.add_parser(command_name, help=help_text)
        add_db(phase2_command)
        phase2_command.add_argument(
            "--payload-stdin",
            action="store_true",
            required=True,
            help="Read the closed JSON request from stdin; body/search text never enters argv",
        )
    context_memory = context_console_sub.add_parser("memory-candidate", help="Create candidate-only MemoryCandidateV1")
    add_db(context_memory)
    context_memory.add_argument("--verified-ref", required=True)
    context_memory.add_argument("--subject-ref", required=True)
    context_prepare = context_console_sub.add_parser("prepare-delivery", help="Prepare a verified-only delivery manifest")
    add_db(context_prepare)
    context_prepare.add_argument("--target-ref", required=True)
    context_prepare.add_argument("--project-ref", default="")
    context_prepare.add_argument("--from", dest="start_at", default="")
    context_prepare.add_argument("--to", dest="end_at", default="")
    context_prepare.add_argument(
        "--query-stdin",
        action="store_true",
        help="Read bounded JSON {query} from stdin; query text is forbidden in argv",
    )
    context_fetch = context_console_sub.add_parser(
        "fetch-delivery",
        help="Fetch the exact frozen public payload by delivery or bundle ref",
    )
    add_db(context_fetch)
    context_fetch.add_argument("--delivery-ref", default="")
    context_fetch.add_argument("--bundle-ref", default="")
    context_delivery = context_console_sub.add_parser("ingest-delivery-receipt", help="Read DeliveryReceiptV1 JSON only from stdin")
    add_db(context_delivery)
    context_delivery.add_argument("--secret-env", default="PAM_CONTEXT_RECEIPT_KEY")
    context_experiment_status = context_console_sub.add_parser("experiment-status", help="Read the natural-day product experiment window")
    add_db(context_experiment_status)
    context_experiment_start = context_console_sub.add_parser("start-experiment", help="Explicitly start, never auto-start, a local product experiment")
    add_db(context_experiment_start)
    context_experiment_start.add_argument("--experiment-ref", required=True)
    context_experiment_start.add_argument("--confirm", required=True)
    context_experiment_finalize = context_console_sub.add_parser(
        "finalize-experiment",
        help="Explicitly freeze an eligible product experiment; never runs from status reads",
    )
    add_db(context_experiment_finalize)
    context_experiment_finalize.add_argument("--experiment-ref", required=True)
    context_experiment_finalize.add_argument(
        "--confirm",
        required=True,
        help=f"Must equal {PRODUCT_EXPERIMENT_FINALIZE_CONFIRMATION}",
    )
    context_baseline = context_console_sub.add_parser("record-baseline", help="Record one independent runtime component baseline")
    add_db(context_baseline)
    context_baseline.add_argument("--component", required=True, choices=("capture", "privacy", "storage", "context_fusion"))
    context_baseline.add_argument("--version-ref", required=True)
    context_baseline.add_argument("--reason-code", required=True)

    document_changes = sub.add_parser("document-changes", help="Manage safe confirmed local document-change evidence")
    document_changes_sub = document_changes.add_subparsers(dest="document_changes_command", required=True)
    document_changes_status_parser = document_changes_sub.add_parser("status", help="Show tracker coverage without local paths")
    add_db(document_changes_status_parser)
    document_changes_list = document_changes_sub.add_parser("list", help="List safe confirmed document changes")
    add_db(document_changes_list)
    document_changes_list.add_argument("--from", dest="start", required=True)
    document_changes_list.add_argument("--to", dest="end", required=True)
    document_changes_list.add_argument("--limit", type=int, default=100)
    document_changes_history = document_changes_sub.add_parser(
        "history",
        help="Read owner-local file history without opening the downstream export gate",
    )
    add_db(document_changes_history)
    document_changes_history.add_argument(
        "--range",
        dest="range_name",
        choices=("today", "yesterday", "7d", "30d", "90d"),
        default="today",
    )
    document_changes_history.add_argument("--from", dest="start")
    document_changes_history.add_argument("--to", dest="end")
    document_changes_history.add_argument("--limit", type=int, default=200)
    document_changes_configure = document_changes_sub.add_parser(
        "configure",
        help="Configure tracker state; raw paths stay only in the private local runtime registry",
    )
    add_db(document_changes_configure)
    document_changes_configure.add_argument("--enabled", choices=("on", "off"))
    document_changes_configure.add_argument("--enable", action="store_true", help=argparse.SUPPRESS)
    document_changes_configure.add_argument("--disable", action="store_true", help=argparse.SUPPRESS)
    document_changes_configure.add_argument("--export-enabled", choices=("on", "off"))
    document_changes_configure.add_argument("--add-root")
    document_changes_configure.add_argument(
        "--reauthorize-root",
        help="Explicitly revoke an unverifiable authorization set and replace it with this root",
    )
    document_changes_configure.add_argument("--root-label")
    document_changes_configure.add_argument("--remove-root")
    document_changes_configure.add_argument("--suspend-root", help="Stop a public root reference without deleting its history")
    document_changes_configure.add_argument("--reset-baseline", action="store_true")

    activity_sessions = sub.add_parser("activity-sessions", help="Summarize unlock-to-lock activity sessions")
    activity_sessions_sub = activity_sessions.add_subparsers(dest="activity_sessions_command", required=True)
    activity_sessions_current = activity_sessions_sub.add_parser("current", help="Show the current unlock session")
    add_db(activity_sessions_current)
    activity_sessions_current.add_argument("--format", choices=("json", "markdown"), default="json")
    activity_sessions_list = activity_sessions_sub.add_parser("list", help="List unlock sessions for a range")
    add_db(activity_sessions_list)
    activity_sessions_list.add_argument("--range", dest="range_name", choices=("today", "yesterday", "7d"), default="today")
    activity_sessions_list.add_argument("--from", dest="start")
    activity_sessions_list.add_argument("--to", dest="end")
    activity_sessions_list.add_argument("--limit", type=int, default=20)
    activity_sessions_list.add_argument("--format", choices=("json", "markdown"), default="json")
    activity_sessions_show = activity_sessions_sub.add_parser("show", help="Show one unlock session")
    add_db(activity_sessions_show)
    activity_sessions_show.add_argument("session_id")
    activity_sessions_show.add_argument("--format", choices=("json", "markdown"), default="json")

    ai_handoff = sub.add_parser("ai-handoff", help="Generate read-only clear-only AI handoff packages")
    ai_handoff_sub = ai_handoff.add_subparsers(dest="ai_handoff_command", required=True)
    ai_handoff_status_parser = ai_handoff_sub.add_parser("status", help="Show read-only AI handoff interface status")
    add_db(ai_handoff_status_parser)
    ai_handoff_package = ai_handoff_sub.add_parser("package", help="Print a read-only AI handoff package")
    add_db(ai_handoff_package)
    ai_handoff_package.add_argument("--range", dest="range_name", choices=("today", "yesterday", "7d"), default="today")
    ai_handoff_package.add_argument("--from", dest="start")
    ai_handoff_package.add_argument("--to", dest="end")
    ai_handoff_package.add_argument("--format", choices=("json", "markdown"), default="json")
    ai_handoff_package.add_argument("--max-items", type=int, default=40)
    ai_handoff_export = ai_handoff_sub.add_parser("export", help="Write a read-only AI handoff package to a local file")
    add_db(ai_handoff_export)
    ai_handoff_export.add_argument("--range", dest="range_name", choices=("today", "yesterday", "7d"), default="today")
    ai_handoff_export.add_argument("--from", dest="start")
    ai_handoff_export.add_argument("--to", dest="end")
    ai_handoff_export.add_argument("--format", choices=("json", "markdown"), default="markdown")
    ai_handoff_export.add_argument("--max-items", type=int, default=40)
    ai_handoff_export.add_argument("--out", required=True, help="Write the handoff package to this local path")

    pkg = sub.add_parser("package", help="Generate an HA Source Package")
    add_db(pkg)
    pkg.add_argument("--from", dest="start", required=True)
    pkg.add_argument("--to", dest="end", required=True)
    pkg.add_argument("--out", default=DEFAULT_PACKAGE_DIR)
    pkg.add_argument("--no-markdown", action="store_true")

    gp = sub.add_parser("get-package", help="Read an HA package through the safe wrapper")
    add_db(gp)
    gp.add_argument("package_id")
    gp.add_argument("--handoff-summary", action="store_true", help="Return a compact Hermes handoff summary instead of the full package")
    gp.add_argument("--memory-candidates-only", action="store_true", help="Return only compact memory candidates for Hermes handoff")
    gp.add_argument("--max-items", type=int, default=40, help="Maximum summary items for compact package outputs")

    rp = sub.add_parser("revoke-package", help="Revoke one HA Source Package cache without deleting activities")
    add_db(rp)
    rp.add_argument("package_id")
    rp.add_argument("--reason", default="manual_revoked")

    list_pkg = sub.add_parser("list-packages", help="List HA source package records")
    add_db(list_pkg)

    dr = sub.add_parser("delete-range", help="Delete a time range and revoke overlapping packages")
    add_db(dr)
    dr.add_argument("--from", dest="start", required=True)
    dr.add_argument("--to", dest="end", required=True)
    dr.add_argument("--reason", default="user_deleted")

    expire = sub.add_parser("expire-packages", help="Expire packages past retention_until")
    add_db(expire)

    demo = sub.add_parser("demo", help="Initialize, ingest sample data, and generate one package")
    add_db(demo)
    demo.add_argument("--file", default="samples/sample_events.jsonl")
    demo.add_argument("--out", default=DEFAULT_PACKAGE_DIR)

    cap = sub.add_parser("capture-once", help="Capture one daemon-governed keyframe round")
    add_db(cap)
    cap.add_argument("--with-screenshot-ocr", action="store_true")
    cap.add_argument("--debug-bypass-daemon-state", action="store_true")

    loop = sub.add_parser("capture-loop", help="Capture repeated daemon-governed keyframe rounds")
    add_db(loop)
    loop.add_argument("--count", type=int, default=6)
    loop.add_argument("--interval", type=float, default=10.0)
    loop.add_argument("--with-screenshot-ocr", action="store_true")
    loop.add_argument("--debug-bypass-daemon-state", action="store_true")

    sck = sub.add_parser("capture-keyframes", help="Capture one ScreenCaptureKit keyframe per display, OCR, then delete images")
    add_db(sck)
    sck.add_argument("--debug-bypass-daemon-state", action="store_true")

    daemon = sub.add_parser("run-daemon", help="Run the 15-second local capture daemon")
    add_db(daemon)
    daemon.add_argument("--once", action="store_true", help="Run one daemon capture round and exit")
    daemon.add_argument("--interval", type=int, help="Override capture interval seconds")
    daemon.add_argument("--dev-dashboard", action="store_true", help="Start the development dashboard with the daemon")

    status = sub.add_parser("status", help="Show daemon and dashboard status")
    add_db(status)
    status.add_argument("--quick", action="store_true", help="Read a lightweight, content-free status snapshot")

    sharing = sub.add_parser("codex-sharing", help="Owner-local confirmed context sharing; never exposed as an MCP mutation")
    add_db(sharing)
    sharing.add_argument("--action", required=True, choices=("snapshot", "manual-note", "confirm-card", "share", "withdraw", "feedback"))
    sharing.add_argument("--payload-stdin", action="store_true", required=True)
    sharing = sub.add_parser("agi-sharing", help="Owner-local confirmed context sharing; never exposed as an MCP mutation")
    add_db(sharing)
    sharing.add_argument("--action", required=True, choices=("snapshot", "manual-note", "confirm-card", "share", "withdraw", "feedback"))
    sharing.add_argument("--payload-stdin", action="store_true", required=True)

    screen_capture = sub.add_parser(
        "configure-screen-capture",
        help="Enable or disable screen capture without pausing file tracking",
    )
    add_db(screen_capture)
    screen_capture.add_argument("--enabled", required=True, choices=("on", "off"))

    soak = sub.add_parser("soak", help="Track and report the content-free 7-day stability gate")
    soak_sub = soak.add_subparsers(dest="soak_command", required=True)
    soak_status = soak_sub.add_parser("status", help="Show the current 7-day stability scorecard")
    add_db(soak_status)
    soak_status.add_argument("--days", type=int, default=7)
    soak_status.add_argument("--format", choices=("json", "markdown"), default="json")
    soak_snapshot = soak_sub.add_parser("snapshot", help="Refresh today's content-free soak snapshot")
    add_db(soak_snapshot)
    soak_snapshot.add_argument("--format", choices=("json", "markdown"), default="json")
    soak_baseline = soak_sub.add_parser("baseline", help="Record a content-free resource baseline from a previous version")
    add_db(soak_baseline)
    soak_baseline.add_argument("--version", required=True)
    soak_baseline.add_argument("--cpu-percent", type=float)
    soak_baseline.add_argument("--rss-bytes", type=int)
    soak_baseline.add_argument("--disk-bytes", type=int)
    soak_win = soak_sub.add_parser("record-win", help="Record one user-confirmed context recovery win")
    add_db(soak_win)
    soak_win.add_argument("--minutes-saved", type=int, default=0)
    soak_drill = soak_sub.add_parser("record-drill", help="Record the result of a user-run safety control drill")
    add_db(soak_drill)
    soak_drill.add_argument("--kind", required=True, choices=REQUIRED_DRILLS)
    soak_drill.add_argument("--result", required=True, choices=("passed", "failed", "skipped"))




    pa = sub.add_parser("pause", help="Pause capture without stopping the dashboard")
    add_db(pa)
    pa.add_argument("--minutes", type=int, default=30)
    pa.add_argument("--until-lock", action="store_true", help="Pause until the next lock event is detected")
    pa.add_argument("--today", action="store_true", help="Pause until the next local day")
    pa.add_argument("--manual", action="store_true", help="Pause until recording is manually resumed")

    re = sub.add_parser("resume", help="Resume capture after a pause")
    add_db(re)

    clean = sub.add_parser("cleanup-retention", help="Preview retention cleanup; apply only with explicit confirmation")
    add_db(clean)
    clean.add_argument("--days", type=int, help="Override with a rolling day window")
    clean.add_argument("--apply", action="store_true", help="Actually delete expired local activity data")
    clean.add_argument(
        "--confirm-retention-delete",
        default="",
        help="Required with --apply: DELETE_EXPIRED_LOCAL_ACTIVITY",
    )

    rc = sub.add_parser("reclassify", help="Re-run current privacy rules over stored clear activities")
    add_db(rc)
    rc.add_argument("--from", dest="start")
    rc.add_argument("--to", dest="end")
    rc.add_argument("--dry-run", action="store_true", help="Only count affected records; do not delete, update, or revoke packages")

    identity_collisions = sub.add_parser(
        "document-signal-collisions",
        help="Preview, apply, or reverse deterministic App identity collision markers",
    )
    add_db(identity_collisions)
    identity_collisions.add_argument(
        "--mode",
        choices=("dry-run", "preview", "apply", "reverse"),
        default="dry-run",
    )
    identity_collisions.add_argument("--confirm-hash", default="", help=argparse.SUPPRESS)
    identity_collisions.add_argument("--manifest-hash", default="", help=argparse.SUPPRESS)
    identity_collisions.add_argument(
        "--historical-cutoff",
        default="",
        help="UTC-aware freeze timestamp captured after the old Core is stopped",
    )

    bh = sub.add_parser("build-helpers", help="Compile Swift helpers into data/bin")
    add_db(bh)

    browser_hosts = sub.add_parser("install-browser-hosts", help="Install local Chromium native host manifests")
    add_db(browser_hosts)
    browser_hosts.add_argument("--browser", action="append", choices=("dia", "chrome", "arc"))
    browser_hosts.add_argument("--extension-id", default="")

    native_host = sub.add_parser("browser-native-host", help=argparse.SUPPRESS)
    native_host.add_argument("--core-root")

    la = sub.add_parser("install-launch-agent", help="Install and start the LaunchAgent daemon")
    add_db(la)
    la.add_argument("--privacy-profile", choices=("strict", "personal_high_precision"))
    la.add_argument(
        "--preserve-privacy-profile",
        action="store_true",
        help="Preserve the currently installed LaunchAgent privacy profile when reinstalling",
    )

    ula = sub.add_parser("uninstall-launch-agent", help="Uninstall the LaunchAgent daemon")
    add_db(ula)

    sv = sub.add_parser("serve", help="Run the local debug dashboard")
    add_db(sv)
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)

    hygiene = sub.add_parser("data-hygiene", help="List local data, backup, and legacy locations without deleting")
    add_db(hygiene)
    backup_hygiene = sub.add_parser("backup-hygiene", help="List backup/runtime-data cleanup candidates without deleting")
    add_db(backup_hygiene)
    hygiene_cleanup = sub.add_parser("hygiene-cleanup", help="Dry-run old data hygiene cleanup; apply only with explicit confirmation")
    add_db(hygiene_cleanup)
    hygiene_cleanup.add_argument("--apply", action="store_true", help="Delete invalid HA cache residue and rotate old backups")
    hygiene_cleanup.add_argument(
        "--confirm-hygiene-cleanup",
        default="",
        help=f"Required with --apply: {HYGIENE_CLEANUP_CONFIRMATION}",
    )
    hygiene_cleanup.add_argument("--keep-backups", type=int, default=DEFAULT_KEEP_BACKUPS)
    hygiene_cleanup.add_argument("--min-age-days", type=int, default=DEFAULT_MIN_AGE_DAYS)
    hygiene_cleanup.add_argument("--quarantine-days", type=int, default=DEFAULT_QUARANTINE_DAYS)
    hygiene_cleanup.add_argument("--no-ha-cache", action="store_true", help="Skip invalid HA cache residue cleanup")
    hygiene_cleanup.add_argument("--no-backup-rotation", action="store_true", help="Skip old backup quarantine rotation")
    hygiene_cleanup.add_argument("--no-export-cache", action="store_true", help="Skip old cleaned/learning export cache cleanup")
    hygiene_cleanup.add_argument("--no-permissions", action="store_true", help="Skip core/data runtime permission normalization")

    configure_hygiene = sub.add_parser("configure-hygiene-cleanup", help="Configure daemon old-data hygiene automation")
    add_db(configure_hygiene)
    configure_hygiene.add_argument("--auto", choices=("on", "off"))
    configure_hygiene.add_argument("--backup-rotation", choices=("on", "off"))
    configure_hygiene.add_argument("--keep-backups", type=int)
    configure_hygiene.add_argument("--min-age-days", type=int)
    configure_hygiene.add_argument("--quarantine-days", type=int)

    configure_privacy = sub.add_parser("configure-privacy-profile", help="Configure local privacy precision profile")
    add_db(configure_privacy)
    configure_privacy.add_argument("--profile", required=True, choices=("strict", "personal_high_precision"))

    privacy_policy = sub.add_parser("privacy-policy", help="Inspect Privacy Firewall policy without changing rules")
    privacy_policy_sub = privacy_policy.add_subparsers(dest="privacy_policy_command", required=True)
    privacy_policy_status_parser = privacy_policy_sub.add_parser("status", help="Show read-only privacy policy status")
    add_db(privacy_policy_status_parser)
    privacy_policy_preview_parser = privacy_policy_sub.add_parser("preview", help="Preview how an app/domain would be handled")
    add_db(privacy_policy_preview_parser)
    privacy_policy_preview_parser.add_argument("--app", default="")
    privacy_policy_preview_parser.add_argument("--domain", default="")
    privacy_policy_preview_parser.add_argument("--title", default="")
    privacy_policy_preview_parser.add_argument("--text", default="")
    privacy_profile_preview_parser = privacy_policy_sub.add_parser("profile-preview", help="Dry-run a built-in privacy profile switch")
    add_db(privacy_profile_preview_parser)
    privacy_profile_preview_parser.add_argument("--profile", required=True, choices=("strict", "personal_high_precision"))

    diagnostics = sub.add_parser("diagnostics", help="Show safe local diagnostic status")
    diagnostics_sub = diagnostics.add_subparsers(dest="diagnostics_command", required=True)
    diagnostics_status_parser = diagnostics_sub.add_parser("status", help="Show the current local diagnostic summary")
    add_db(diagnostics_status_parser)
    diagnostics_status_parser.add_argument("--format", choices=("json", "markdown"), default="json")
    diagnostics_status_parser.add_argument("--out", help="Optional output path for markdown diagnostics")
    diagnostics_status_parser.add_argument("--mode", choices=("quick", "full"), default="full")
    diagnostics_status_parser.add_argument("--quick", action="store_true", help="Use the fast diagnostic path")

    ai_connector = sub.add_parser("ai-connector", help="Expose safe read-only AI connector over loopback")
    ai_connector_sub = ai_connector.add_subparsers(dest="ai_connector_command", required=True)
    ai_connector_status_parser = ai_connector_sub.add_parser("status", help="Show AI connector safety and endpoint status")
    add_db(ai_connector_status_parser)
    ai_connector_status_parser.add_argument("--host", default=DEFAULT_AI_CONNECTOR_HOST)
    ai_connector_status_parser.add_argument("--port", type=int, default=DEFAULT_AI_CONNECTOR_PORT)
    ai_connector_start_parser = ai_connector_sub.add_parser("start", help="Start the managed read-only loopback AI connector")
    add_db(ai_connector_start_parser)
    ai_connector_start_parser.add_argument("--host", default=DEFAULT_AI_CONNECTOR_HOST)
    ai_connector_start_parser.add_argument("--port", type=int, default=DEFAULT_AI_CONNECTOR_PORT)
    ai_connector_stop_parser = ai_connector_sub.add_parser("stop", help="Stop the managed read-only loopback AI connector")
    add_db(ai_connector_stop_parser)
    ai_connector_stop_parser.add_argument("--host", default=DEFAULT_AI_CONNECTOR_HOST)
    ai_connector_stop_parser.add_argument("--port", type=int, default=DEFAULT_AI_CONNECTOR_PORT)
    ai_connector_package_parser = ai_connector_sub.add_parser("package", help="Print a safe AI handoff package through the connector contract")
    add_db(ai_connector_package_parser)
    ai_connector_package_parser.add_argument("--range", dest="range_name", default="today", choices=("today", "yesterday", "7d"))
    ai_connector_package_parser.add_argument("--format", choices=("json", "markdown"), default="json")
    ai_connector_package_parser.add_argument("--max-items", type=int, default=40)
    ai_connector_serve_parser = ai_connector_sub.add_parser("serve", help="Run the read-only loopback AI connector")
    add_db(ai_connector_serve_parser)
    ai_connector_serve_parser.add_argument("--host", default=DEFAULT_AI_CONNECTOR_HOST)
    ai_connector_serve_parser.add_argument("--port", type=int, default=DEFAULT_AI_CONNECTOR_PORT)

    configure_ha = sub.add_parser("configure-ha-automation", help="Configure hourly HA package automation")
    add_db(configure_ha)
    configure_ha.add_argument("--auto", choices=("on", "off"))
    configure_ha.add_argument("--interval-minutes", type=int)
    configure_ha.add_argument("--trigger-mode", choices=("direct_hermes",))
    configure_ha.add_argument("--hermes-profile")

    run_ha = sub.add_parser("run-ha-automation-once", help="Generate and hand off one hourly HA package if due")
    add_db(run_ha)
    run_ha.add_argument("--force", action="store_true", help="Bypass the live-runtime guard for explicit tests")
    run_ha.add_argument("--no-trigger", action="store_true", help="Generate/queue without invoking Hermes")

    ha_status = sub.add_parser("ha-automation-status", help="Show hourly HA automation status")
    add_db(ha_status)

    validate_ha = sub.add_parser("validate-ha-automation", help="Validate hourly HA automation dependencies")
    add_db(validate_ha)

    configure_quality_advice_parser = sub.add_parser("configure-quality-advice", help="Configure daily Hermes quality advice")
    add_db(configure_quality_advice_parser)
    configure_quality_advice_parser.add_argument("--auto", choices=("on", "off"))
    configure_quality_advice_parser.add_argument("--profile")
    configure_quality_advice_parser.add_argument("--interval-hours", type=int)

    quality_advice_parser = sub.add_parser("quality-advice", help="Run or inspect Hermes quality advice")
    quality_advice_sub = quality_advice_parser.add_subparsers(dest="quality_advice_command", required=True)
    quality_advice_run = quality_advice_sub.add_parser("run", help="Generate safe quality inputs and ask Hermes for advice")
    add_db(quality_advice_run)
    quality_advice_run.add_argument("--from", dest="start")
    quality_advice_run.add_argument("--to", dest="end")
    quality_advice_run.add_argument("--force", action="store_true", help="Bypass live-runtime and duplicate guards for explicit tests")
    quality_advice_run.add_argument("--no-trigger", action="store_true", help="Generate safe inputs without invoking Hermes")
    quality_advice_status_parser = quality_advice_sub.add_parser("status", help="Show Hermes quality advice status")
    add_db(quality_advice_status_parser)
    validate_quality_advice_parser = quality_advice_sub.add_parser("validate", help="Validate Hermes quality advice dependencies")
    add_db(validate_quality_advice_parser)

    qr = sub.add_parser("quality-report", help="Show local cleaning and learning quality diagnostics")
    add_db(qr)
    qr.add_argument("--from", dest="start")
    qr.add_argument("--to", dest="end")
    qr.add_argument("--format", choices=("json", "markdown"), default="json")
    qr.add_argument("--out", help="Write the quality report to a local file")

    ma = sub.add_parser("memory-audit", help="Read-only audit of PAM/Hermes long-term memories")
    add_db(ma)
    ma.add_argument("--from", dest="start")
    ma.add_argument("--to", dest="end")
    ma.add_argument("--format", choices=("json", "markdown"), default="json")
    ma.add_argument("--out", help="Write the memory audit to a local file")
    ma.add_argument("--limit", type=int, default=200)

    ledger = sub.add_parser(
        "memory-ledger",
        help="Inspect the frozen read-only legacy PAM memory ledger",
        description="Inspect the frozen read-only legacy PAM memory ledger.",
    )
    ledger_sub = ledger.add_subparsers(dest="memory_ledger_command", required=True)
    ledger_refresh = ledger_sub.add_parser("refresh", help="Report frozen-ledger status; no files are regenerated")
    add_db(ledger_refresh)
    ledger_refresh.add_argument("--path", help="Override the ledger Markdown path")
    ledger_refresh.add_argument("--detail-path", help="Override the ledger detail Markdown path")
    ledger_refresh.add_argument("--limit", type=int, default=1000)
    ledger_sync = ledger_sub.add_parser("sync", help="Report frozen-ledger status; no changes are applied")
    add_db(ledger_sync)
    ledger_sync.add_argument("--path", help="Override the ledger Markdown path")
    ledger_sync.add_argument("--detail-path", help="Override the ledger detail Markdown path")
    ledger_sync.add_argument("--backup-dir", help="Override the sync backup directory")
    ledger_sync.add_argument("--dry-run", action="store_true")
    ledger_status = ledger_sub.add_parser("status", help="Show PAM memory ledger status")
    add_db(ledger_status)
    ledger_status.add_argument("--path", help="Override the ledger Markdown path")
    ledger_status.add_argument("--detail-path", help="Override the ledger detail Markdown path")

    approval = sub.add_parser(
        "memory-approval",
        help="Inspect approvals; the controlled memory writer is disabled",
        description="Inspect approvals; the controlled memory writer is disabled.",
    )
    approval_sub = approval.add_subparsers(dest="memory_approval_command", required=True)
    approval_status = approval_sub.add_parser("status", help="Show safe memory approval console status")
    add_db(approval_status)
    approval_status.add_argument("--package", default="latest")
    approval_status.add_argument("--limit", type=int, default=40)
    approval_candidates = approval_sub.add_parser("candidates", help="Show safe HA memory candidates for approval")
    add_db(approval_candidates)
    approval_candidates.add_argument("--package", default="latest")
    approval_candidates.add_argument("--limit", type=int, default=40)
    approval_preview = approval_sub.add_parser("preview", help="Return a fail-closed refusal while the writer is disabled")
    add_db(approval_preview)
    approval_preview.add_argument("--action", required=True, choices=("delete", "rewrite", "approve-candidate", "promote-review-only"))
    approval_preview.add_argument("--ledger-id", default="")
    approval_preview.add_argument("--replacement-text", default="")
    approval_preview.add_argument("--package", default="latest")
    approval_preview.add_argument("--candidate-key", default="")
    approval_preview.add_argument("--memory-text", default="")
    approval_apply = approval_sub.add_parser("apply", help="Return a fail-closed refusal while the writer is disabled")
    add_db(approval_apply)
    approval_apply.add_argument("--plan-id", required=True)
    approval_records = approval_sub.add_parser("records", help="Show recent local memory approval records")
    approval_records.add_argument("--limit", type=int, default=20)

    review = sub.add_parser(
        "memory-review",
        help="Review the shared long-term memory backlog and record lifecycle decisions",
        description=(
            "Review the shared long-term memory backlog. Decisions are written to the "
            "governance ledger that Mem0 retrieval already reads; no memory is deleted."
        ),
    )
    review_sub = review.add_subparsers(dest="memory_review_command", required=True)
    review_status = review_sub.add_parser("status", help="Summarize the backlog and its source artifacts")
    review_status.add_argument("--queue-path", default=None)
    review_status.add_argument("--ledger-path", default=None)
    review_list = review_sub.add_parser("list", help="List queued memories and their current decision")
    review_list.add_argument("--state", choices=("all", "pending", "decided"), default="all")
    review_list.add_argument("--limit", type=int, default=200)
    review_list.add_argument("--queue-path", default=None)
    review_list.add_argument("--ledger-path", default=None)
    review_reveal = review_sub.add_parser("reveal", help="Reveal one memory's text on demand; nothing is stored")
    review_reveal.add_argument("--memory-id", required=True)
    review_reveal.add_argument("--chroma-path", default=None)
    review_preview = review_sub.add_parser("preview", help="Validate a decision batch and return its confirmation digest")
    review_preview.add_argument("--decisions", required=True, help="JSON array of {memory_id, action, note}")
    review_preview.add_argument("--queue-path", default=None)
    review_preview.add_argument("--ledger-path", default=None)
    review_apply = review_sub.add_parser("apply", help="Write a previewed decision batch to the governance ledger")
    review_apply.add_argument("--decisions", required=True, help="JSON array of {memory_id, action, note}")
    review_apply.add_argument("--confirm-digest", required=True, help="preview_digest returned by memory-review preview")
    review_apply.add_argument("--queue-path", default=None)
    review_apply.add_argument("--ledger-path", default=None)
    review_nominate = review_sub.add_parser(
        "nominate",
        help="Nominate one outdated memory for Local Owner's review; decides nothing",
        description=(
            "Record an AI nomination with evidence. The nominator identity comes from the "
            "calling process, the memory's current content hash is bound, and retrieval "
            "does not change until Local Owner applies a decision. Do not paste memory text."
        ),
    )
    review_nominate.add_argument("--memory-id", required=True)
    review_nominate.add_argument("--action", required=True, choices=MEMORY_REVIEW_ACTIONS)
    review_nominate.add_argument("--note", required=True, help="Why, in one or two sentences")
    review_nominate.add_argument("--superseded-by", action="append", default=[],
                                 help="ID of an active memory that replaces this one (repeatable)")
    review_nominate.add_argument("--evidence-text", action="append", default=[],
                                 help="Short evidence statement (repeatable)")
    review_nominate.add_argument("--queue-path", default=None)
    review_nominate.add_argument("--ledger-path", default=None)
    for review_parser in (review_status, review_list, review_preview, review_apply, review_nominate):
        review_parser.add_argument("--nominations-path", default=None)
    review_list.add_argument("--no-nominations", action="store_true",
                             help="Show only the audit queue")

    decision_queue = sub.add_parser(
        "decision-queue",
        help="Review safe items and record local-only user decisions",
        description="Review safe items and record local-only user decisions; memory writes remain disabled.",
    )
    decision_queue_sub = decision_queue.add_subparsers(dest="decision_queue_command", required=True)
    decision_queue_list_parser = decision_queue_sub.add_parser("list", help="List safe decision queue items")
    add_db(decision_queue_list_parser)
    decision_queue_list_parser.add_argument("--range", dest="range_name", choices=("today", "yesterday", "7d"), default="today")
    decision_queue_list_parser.add_argument("--start", default=None)
    decision_queue_list_parser.add_argument("--end", default=None)
    decision_queue_list_parser.add_argument("--package", default="latest")
    decision_queue_list_parser.add_argument("--limit", type=int, default=80)
    decision_queue_list_parser.add_argument("--format", choices=("json",), default="json")
    decision_queue_list_parser.add_argument("--include-decided", action="store_true")
    decision_queue_record = decision_queue_sub.add_parser("record", help="Record a context-only, never-write, or local feedback decision")
    add_db(decision_queue_record)
    decision_queue_record.add_argument("--item-key", required=True)
    decision_queue_record.add_argument(
        "--action",
        required=True,
        choices=("context-only", "never-write", "folder-sidebar", "old-task", "reference-material", "correct"),
    )
    decision_queue_record.add_argument("--note", default="")
    decision_queue_record.add_argument("--generalize", choices=("exact", "similar"), default="similar")
    decision_queue_record.add_argument("--range", dest="range_name", choices=("today", "yesterday", "7d"), default="today")
    decision_queue_record.add_argument("--start", default=None)
    decision_queue_record.add_argument("--end", default=None)
    decision_queue_record.add_argument("--package", default="latest")
    decision_queue_preview = decision_queue_sub.add_parser("preview-write", help="Return a fail-closed refusal while the writer is disabled")
    add_db(decision_queue_preview)
    decision_queue_preview.add_argument("--item-key", required=True)
    decision_queue_preview.add_argument("--memory-text", default="")
    decision_queue_preview.add_argument("--range", dest="range_name", choices=("today", "yesterday", "7d"), default="today")
    decision_queue_preview.add_argument("--start", default=None)
    decision_queue_preview.add_argument("--end", default=None)
    decision_queue_preview.add_argument("--package", default="latest")
    decision_queue_apply = decision_queue_sub.add_parser("apply", help="Return a fail-closed refusal while the writer is disabled")
    add_db(decision_queue_apply)
    decision_queue_apply.add_argument("--plan-id", required=True)

    activity_confirmation = sub.add_parser(
        "activity-confirmation",
        help="Review uncertain real-world activity occurrences and record local calibration",
    )
    activity_confirmation_sub = activity_confirmation.add_subparsers(
        dest="activity_confirmation_command",
        required=True,
    )
    activity_confirmation_list = activity_confirmation_sub.add_parser(
        "list",
        help="List safe occurrence questions without raw screenshots or OCR",
    )
    add_db(activity_confirmation_list)
    activity_confirmation_list.add_argument(
        "--range",
        dest="range_name",
        choices=("today", "yesterday", "7d"),
        default="today",
    )
    activity_confirmation_list.add_argument("--start", default=None)
    activity_confirmation_list.add_argument("--end", default=None)
    activity_confirmation_list.add_argument("--limit", type=int, default=80)
    activity_confirmation_list.add_argument("--format", choices=("json",), default="json")
    activity_confirmation_list.add_argument("--include-resolved", action="store_true")
    activity_confirmation_cards = activity_confirmation_sub.add_parser(
        "cards",
        help="List one fast, unresolved-only page using the ConfirmationCardV5 contract",
    )
    add_db(activity_confirmation_cards)
    activity_confirmation_cards.add_argument(
        "--range",
        dest="range_name",
        choices=("today", "yesterday", "7d"),
        default="today",
    )
    activity_confirmation_cards.add_argument("--start", default=None)
    activity_confirmation_cards.add_argument("--end", default=None)
    activity_confirmation_cards.add_argument("--limit", type=int, default=12)
    activity_confirmation_cards.add_argument("--cursor", default=None)
    activity_confirmation_cards.add_argument(
        "--section",
        choices=("pending_confirmation", "information_insufficient"),
        default=None,
    )
    activity_confirmation_cards.add_argument("--format", choices=("json",), default="json")
    activity_confirmation_revision_parser = activity_confirmation_sub.add_parser(
        "revision",
        help="Return the lightweight unresolved-card revision token",
    )
    add_db(activity_confirmation_revision_parser)
    activity_confirmation_revision_parser.add_argument(
        "--range",
        dest="range_name",
        choices=("today", "yesterday", "7d"),
        default="today",
    )
    activity_confirmation_revision_parser.add_argument("--start", default=None)
    activity_confirmation_revision_parser.add_argument("--end", default=None)
    activity_confirmation_revision_parser.add_argument("--format", choices=("json",), default="json")
    activity_confirmation_revalidate = activity_confirmation_sub.add_parser(
        "revalidate",
        help="Preview or explicitly apply the current confirmation policy to unresolved rows",
    )
    add_db(activity_confirmation_revalidate)
    activity_confirmation_revalidate.add_argument(
        "--apply",
        action="store_true",
        help="Apply the previewed reclassification; the default is rollback-only dry-run",
    )
    activity_confirmation_answer = activity_confirmation_sub.add_parser(
        "answer",
        help="Answer one occurrence question and optionally generalize the feedback",
    )
    add_db(activity_confirmation_answer)
    activity_confirmation_answer.add_argument("--item-key", required=True)
    activity_confirmation_answer.add_argument(
        "--answer",
        required=True,
        choices=("occurred", "not_occurred", "ignored", "misrecognized", "defer"),
    )
    activity_confirmation_answer.add_argument(
        "--reason",
        default="other",
        choices=tuple(sorted(ACTIVITY_CONFIRMATION_REASONS)),
    )
    activity_confirmation_answer.add_argument(
        "--generalize",
        choices=("exact", "similar"),
        default="exact",
    )
    activity_confirmation_answer_note = activity_confirmation_answer.add_mutually_exclusive_group()
    activity_confirmation_answer_note.add_argument("--note", default="", help=argparse.SUPPRESS)
    activity_confirmation_answer_note.add_argument(
        "--note-stdin",
        action="store_true",
        help="Read the optional local note from stdin so it is not exposed in process arguments",
    )
    activity_confirmation_answer_group = activity_confirmation_sub.add_parser(
        "answer-group",
        help="Atomically answer one merged occurrence question",
    )
    add_db(activity_confirmation_answer_group)
    activity_confirmation_answer_group.add_argument("--group-key", required=True)
    activity_confirmation_answer_group.add_argument("--member-revision", required=True)
    activity_confirmation_answer_group.add_argument(
        "--answer",
        required=True,
        choices=("occurred", "not_occurred", "ignored", "misrecognized", "defer"),
    )
    activity_confirmation_answer_group.add_argument(
        "--reason",
        default="other",
        choices=tuple(sorted(ACTIVITY_CONFIRMATION_REASONS)),
    )
    activity_confirmation_answer_group.add_argument(
        "--generalize",
        choices=("exact", "similar"),
        default="exact",
    )
    activity_confirmation_answer_group_note = activity_confirmation_answer_group.add_mutually_exclusive_group()
    activity_confirmation_answer_group_note.add_argument("--note", default="", help=argparse.SUPPRESS)
    activity_confirmation_answer_group_note.add_argument(
        "--note-stdin",
        action="store_true",
        help="Read the optional local note from stdin so it is not exposed in process arguments",
    )
    activity_confirmation_answer_group_note.add_argument(
        "--details-stdin",
        action="store_true",
        help="Read bounded JSON {note,workstream,outcome} from stdin",
    )
    activity_confirmation_clarify = activity_confirmation_sub.add_parser(
        "clarify",
        help="Re-evaluate one question from retained local evidence, fresh OCR, and bounded cleaned context",
    )
    add_db(activity_confirmation_clarify)
    activity_confirmation_clarify.add_argument("--group-key", required=True)
    activity_confirmation_clarify.add_argument("--member-revision", required=True)
    activity_confirmation_clarify_note = activity_confirmation_clarify.add_mutually_exclusive_group()
    activity_confirmation_clarify_note.add_argument("--note", default="", help=argparse.SUPPRESS)
    activity_confirmation_clarify_note.add_argument(
        "--note-stdin",
        action="store_true",
        help="Read the optional local note from stdin so it is not exposed in process arguments",
    )
    activity_confirmation_revoke = activity_confirmation_sub.add_parser(
        "revoke-calibration",
        help="Revoke one learned calibration rule without changing answer history",
    )
    add_db(activity_confirmation_revoke)
    activity_confirmation_revoke.add_argument("--calibration-key", required=True)
    activity_confirmation_configure = activity_confirmation_sub.add_parser(
        "configure",
        help="Configure pending-question notification density",
    )
    add_db(activity_confirmation_configure)
    activity_confirmation_configure.add_argument(
        "--mode",
        required=True,
        choices=("compact", "standard", "max_recall"),
    )
    activity_confirmation_manual = activity_confirmation_sub.add_parser(
        "manual-add",
        help="Add one explicitly confirmed safe manual fact from bounded JSON stdin",
    )
    add_db(activity_confirmation_manual)
    activity_confirmation_manual.add_argument("--confirm", required=True)
    activity_confirmation_manual.add_argument("--payload-stdin", action="store_true", required=True)
    activity_confirmation_settle = activity_confirmation_sub.add_parser(
        "settle",
        help="Materialize and persist one content-free daily confirmation settlement",
    )
    add_db(activity_confirmation_settle)
    activity_confirmation_settle.add_argument("--date", required=True)
    activity_confirmation_settle.add_argument("--format", choices=("json",), default="json")

    qf = sub.add_parser("quality-feedback", help="Add or list local quality feedback metadata")
    qf_sub = qf.add_subparsers(dest="quality_feedback_command", required=True)
    qf_add = qf_sub.add_parser("add", help="Add feedback for a cleaned record")
    add_db(qf_add)
    qf_add.add_argument("--target-type", required=True, choices=("activity", "privacy_event", "gap_event", "document_signal"))
    qf_add.add_argument("--target-id", required=True)
    qf_add.add_argument(
        "--issue-type",
        required=True,
        choices=(
            "false_block",
            "false_clear",
            "over_redacted",
            "under_redacted",
            "wrong_workflow",
            "missed_document_signal",
            "duplicate_error",
            "low_signal",
        ),
    )
    qf_add.add_argument("--expected", required=True, choices=("clear", "partial_redacted", "blocked", "gap", "duplicate"))
    qf_add.add_argument("--note", default="")
    qf_list = qf_sub.add_parser("list", help="List local quality feedback")
    add_db(qf_list)
    qf_list.add_argument("--from", dest="start")
    qf_list.add_argument("--to", dest="end")

    attention = sub.add_parser("attention-center", help="AGI decision workspace")
    attention.add_argument("arguments", nargs=argparse.REMAINDER)

    closure = sub.add_parser("task-closure", help="Closure Steward private task ledger")
    closure.add_argument("arguments", nargs=argparse.REMAINDER)

    args = parser.parse_args()
    global _DISPLAY_ROOT
    _DISPLAY_ROOT = Path(args.db).expanduser().absolute().parent if args.command in {
        "status", "daily-review", "review", "search", "timeline", "activity-sessions", "activity-confirmation", "memory-review", "context-console", "agi-sharing"
    } else None
    if args.command == "task-closure":
        from .task_closure import main as closure_main
        closure_main(args.arguments, database=args.db)
        return
    if args.command == "attention-center":
        from .attention_center import main as attention_main
        attention_main(args.arguments, database=args.db)
        return
    if args.command == "context-console" and args.context_console_command == "status":
        print_json(context_console_status_path(args.db))
        return
    if _phase2_context_read_command(args) and not Path(args.db).expanduser().exists():
        print_json(
            phase2_unknown_contract(
                _phase2_context_contract_version(args),
                "database_missing",
            )
        )
        return
    if args.command == "data-hygiene":
        print_json(data_hygiene_report(Path(__file__).resolve().parents[1], args.db))
        return
    if args.command == "backup-hygiene":
        print_json(backup_hygiene_report(Path(__file__).resolve().parents[1], args.db))
        return
    if args.command == "hygiene-cleanup":
        try:
            report = hygiene_cleanup_report(
                Path(__file__).resolve().parents[1],
                args.db,
                apply=args.apply,
                confirm=args.confirm_hygiene_cleanup,
                keep_backups=args.keep_backups,
                min_age_days=args.min_age_days,
                quarantine_days=args.quarantine_days,
                include_ha_cache=not args.no_ha_cache,
                include_backup_rotation=not args.no_backup_rotation,
                include_activity_export_cache=not args.no_export_cache,
                include_runtime_permissions=not args.no_permissions,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print_json(report)
        return

    if _quick_status_command(args) and not Path(args.db).expanduser().exists():
        print_json(_core_quick_status_unavailable_status("database_missing"))
        return

    try:
        conn = _connect_for_command(args)
    except sqlite3.DatabaseError as exc:
        if _phase2_context_read_command(args):
            reason = "database_busy" if _sqlite_busy(exc) else "database_unavailable"
            print_json(
                phase2_unknown_contract(
                    _phase2_context_contract_version(args),
                    reason,
                )
            )
            return
        if _quick_status_command(args):
            print_json(_core_quick_status_unavailable_status("database_unavailable"))
            return
        if _status_busy_command(args) and _sqlite_busy(exc):
            print_json(_connect_busy_status(args))
            return
        raise

    if args.command == "init":
        print_json({"ok": True, "db": str(Path(args.db).resolve())})
        return

    if args.command in {"ingest-sample", "ingest-jsonl"}:
        events = load_jsonl(args.file)
        print_json(ingest_events(conn, events))
        return

    if args.command == "timeline":
        _run_timeline_command(conn, args)
        return

    if args.command == "search":
        if not args.query_stdin:
            raise SystemExit("search requires --query-stdin")
        try:
            query = _context_query_stdin(args)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print_json(search(conn, query, args.app, args.start, args.end, confirmed_only=True))
        return

    if args.command == "daily-review":
        print_json(daily_review(conn, args.start, args.end))
        return

    if args.command == "report-context":
        print_json(report_context(conn, args.report_date))
        return

    if args.command == "context-console":
        command = args.context_console_command
        if command in {
            "snapshot",
            "memory-candidates",
            "delivery-status",
            "local-handoff",
            "owner-action",
            "product-feedback",
        }:
            body = sys.stdin.buffer.read(MAX_CONTRACT_BYTES + 1)
            if command == "snapshot":
                payload = context_snapshot(conn, body)
            elif command == "memory-candidates":
                payload = list_memory_candidates(conn, body)
            elif command == "delivery-status":
                payload = delivery_status(conn, body)
            elif command == "local-handoff":
                payload = build_local_handoff(conn, body)
            elif command == "owner-action":
                payload = owner_context_action(conn, body)
            else:
                payload = record_product_feedback(conn, body)
            print_json(payload)
            return
        if command == "configure":
            print_json(
                configure_context_console(
                    conn,
                    enabled=args.enabled == "on",
                    confirmation=args.confirm,
                )
            )
            return
        if command == "configure-timezone":
            print_json(
                configure_context_local_timezone(
                    conn,
                    timezone_name=args.timezone,
                )
            )
            return
        if command == "register-label":
            label_kwargs = {
                "kind": args.kind,
                "ref": args.ref,
                "label": args.label,
                "project_ref": args.project_ref,
                "subject_scope": args.subject_scope,
            }
            if args.kind == "target":
                label_kwargs.update(
                    {
                        "target_scope": args.target_scope,
                        "allowed_privacy_classes": args.allowed_privacy_classes,
                        "export_policy": args.export_policy,
                    }
                )
            print_json(register_context_label(conn, **label_kwargs))
            return
        if command == "register-principal":
            secret = _context_secret_from_env(args.secret_env)
            print_json(
                register_context_principal(
                    conn,
                    principal_id=args.principal_id,
                    key_id=args.key_id,
                    principal_kind=args.principal_kind,
                    verification_secret=secret,
                    project_ref=args.project_ref,
                    target_ref=args.target_ref,
                    target_scope=args.target_scope,
                )
            )
            return
        if command in {"ingest-receipt", "ingest-evidence", "ingest-delivery-receipt"}:
            body = sys.stdin.buffer.read(MAX_CONTRACT_BYTES + 1)
            secret = _context_secret_from_env(args.secret_env)
            if command == "ingest-receipt":
                payload = ingest_context_receipt(conn, body, verification_secret=secret)
            elif command == "ingest-evidence":
                payload = ingest_controlled_evidence(conn, body, verification_secret=secret)
            else:
                payload = ingest_delivery_receipt(conn, body, verification_secret=secret)
            print_json(payload)
            return
        if command == "bundle":
            print_json(
                build_context_bundle(
                    conn,
                    project_ref=args.project_ref,
                    start_at=args.start_at,
                    end_at=args.end_at,
                    query=_context_query_stdin(args),
                    include_clues=args.include_clues,
                    limit=args.limit,
                )
            )
            return
        if command == "memory-candidate":
            print_json(
                create_memory_candidate(
                    conn,
                    verified_ref=args.verified_ref,
                    subject_ref=args.subject_ref,
                )
            )
            return
        if command == "prepare-delivery":
            print_json(
                prepare_delivery(
                    conn,
                    target_ref=args.target_ref,
                    project_ref=args.project_ref,
                    start_at=args.start_at,
                    end_at=args.end_at,
                    query=_context_query_stdin(args),
                )
            )
            return
        if command == "fetch-delivery":
            print_json(
                fetch_prepared_delivery(
                    conn,
                    delivery_ref=args.delivery_ref,
                    bundle_ref=args.bundle_ref,
                )
            )
            return
        if command == "experiment-status":
            print_json(public_product_experiment_status(conn))
            return
        if command == "start-experiment":
            print_json(
                start_product_experiment(
                    conn,
                    experiment_ref=args.experiment_ref,
                    confirmation=args.confirm,
                )
            )
            return
        if command == "finalize-experiment":
            print_json(
                finalize_product_experiment(
                    conn,
                    experiment_ref=args.experiment_ref,
                    confirmation=args.confirm,
                )
            )
            return
        if command == "record-baseline":
            print_json(
                record_component_baseline(
                    conn,
                    component=args.component,
                    version_ref=args.version_ref,
                    reset_reason_code=args.reason_code,
                )
            )
            return
        raise SystemExit("unsupported context-console command")

    if args.command == "document-changes":
        if args.document_changes_command == "status":
            print_json(document_changes_status(conn))
            return
        if args.document_changes_command == "list":
            # This CLI is the bounded downstream collector interface (Feishu,
            # watchdogs, and scripts), so the shadow/export gate is mandatory.
            print_json(list_document_changes(conn, args.start, args.end, limit=args.limit, for_export=True))
            return
        if args.document_changes_command == "history":
            start, end = resolve_document_change_range(
                args.range_name,
                start=args.start,
                end=args.end,
            )
            payload = list_document_changes(
                conn,
                start,
                end,
                limit=args.limit,
                for_export=False,
                sort_order="recent",
            )
            payload["range"] = args.range_name
            payload["time_range"] = {"start": start, "end": end}
            print_json(payload)
            return
        if args.enable and args.disable:
            raise SystemExit("document-changes configure cannot use --enable and --disable together")
        enabled = (args.enabled == "on") if args.enabled is not None else (True if args.enable else (False if args.disable else None))
        export_enabled = (args.export_enabled == "on") if args.export_enabled is not None else None
        print_json(
            configure_document_changes(
                conn,
                enabled=enabled,
                export_enabled=export_enabled,
                add_root=args.add_root,
                reauthorize_root=args.reauthorize_root,
                root_label=args.root_label,
                remove_root=args.remove_root,
                suspend_root=args.suspend_root,
                reset_baseline=args.reset_baseline,
                runtime_root=Path(args.db).resolve().parent,
            )
        )
        return

    if args.command == "activity-sessions":
        if args.activity_sessions_command == "current":
            payload = get_current_activity_session(conn)
        elif args.activity_sessions_command == "list":
            payload = list_activity_sessions(
                conn,
                range_name=args.range_name,
                start=args.start,
                end=args.end,
                limit=args.limit,
            )
        else:
            payload = show_activity_session(conn, args.session_id)
        from .activity_sessions import display_activity_sessions
        from .presentation_scope import DisplayScope
        payload = display_activity_sessions(conn, payload, DisplayScope(Path(args.db).parent))
        if args.format == "markdown":
            print(render_activity_sessions_markdown(payload), end="")
            return
        print_json(payload)
        return

    if args.command == "ai-handoff":
        if args.ai_handoff_command == "status":
            print_json(ai_handoff_status(conn, Path(args.db).parent))
            return
        package = build_ai_handoff_package(
            conn,
            range_name=args.range_name,
            start=args.start,
            end=args.end,
            max_items=args.max_items,
        )
        if args.ai_handoff_command == "package":
            if args.format == "markdown":
                print(render_ai_handoff_markdown(package), end="")
                return
            print_json(package)
            return
        if args.format == "json":
            content = json.dumps(package, ensure_ascii=False, indent=2)
        else:
            content = render_ai_handoff_markdown(package)
        try:
            print_json(write_ai_handoff_export(args.out, content, package=package, output_format=args.format))
        except ValueError as exc:
            print_json(
                {
                    "status": "blocked",
                    "error": str(exc),
                    "raw_access": False,
                    "destructive_actions": False,
                    "memory_write_actions": False,
                    "cloud_analysis": "off",
                }
            )
        return

    if args.command == "package":
        result = generate_package(conn, args.start, args.end, args.out, not args.no_markdown)
        print_json(
            {
                "package_id": result["package"]["package_id"],
                "activity_count": len(result["package"]["activities"]),
                "json_path": result["json_path"],
                "markdown_path": result["markdown_path"],
            }
        )
        return

    if args.command == "get-package":
        _run_get_package_command(conn, args)
        return

    if args.command == "revoke-package":
        print_json(revoke_package(conn, args.package_id, args.reason))
        return

    if args.command == "list-packages":
        _run_list_packages_command(conn)
        return

    if args.command == "delete-range":
        print_json(delete_range(conn, args.start, args.end, args.reason))
        return

    if args.command == "expire-packages":
        print_json({"expired": expire_packages(conn)})
        return

    if args.command == "demo":
        events = load_jsonl(args.file)
        ingest = ingest_events(conn, events)
        start = min(event["captured_at"] for event in events)
        end_dt = max(parse_time(event["captured_at"]) for event in events)
        end = iso(end_dt + timedelta(minutes=1))
        result = generate_package(conn, start, end, args.out)
        print_json(
            {
                "ingest": ingest,
                "package_id": result["package"]["package_id"],
                "activity_count": len(result["package"]["activities"]),
                "json_path": result["json_path"],
                "markdown_path": result["markdown_path"],
            }
        )
        return

    if args.command == "capture-once":
        # Keep parsing the legacy flag for CLI compatibility, but no command
        # may bypass the current screen grant, pause or session authorization.
        conn.close()
        print_json(run_daemon(args.db, once=True))
        return

    if args.command == "capture-loop":
        conn.close()
        results = []
        for index in range(max(1, args.count)):
            results.append(run_daemon(args.db, once=True))
            if index < args.count - 1:
                time.sleep(max(0.5, args.interval))
        print_json({"rounds": results})
        return

    if args.command == "capture-keyframes":
        conn.close()
        print_json(run_daemon(args.db, once=True))
        return

    if args.command == "run-daemon":
        conn.close()
        result = run_daemon(args.db, once=args.once, interval_seconds=args.interval, dev_dashboard=args.dev_dashboard)
        if result is not None:
            print_json(result)
        return

    if args.command == "status":
        if args.quick:
            try:
                payload = _run_quick_status_command(conn, lambda: get_quick_status(conn))
            except sqlite3.DatabaseError:
                payload = _core_quick_status_unavailable_status("database_incompatible")
            print_json(payload)
            return
        print_json(
            _run_busy_status_command(
                conn,
                lambda: _core_status_current_status(conn, Path(args.db).parent),
                _core_status_busy_status,
            )
        )
        return

    if args.command == "soak":
        root = Path(args.db).resolve().parent
        if args.soak_command == "baseline":
            print_json(
                record_resource_baseline(
                    conn,
                    source_version=args.version,
                    cpu_percent=args.cpu_percent,
                    rss_bytes=args.rss_bytes,
                    disk_bytes=args.disk_bytes,
                )
            )
            return
        if args.soak_command == "record-win":
            print_json(
                record_soak_evidence_event(
                    conn,
                    event_type="context_recovery",
                    result="passed",
                    minutes_saved=args.minutes_saved,
                )
            )
            return
        if args.soak_command == "record-drill":
            print_json(record_soak_evidence_event(conn, event_type=args.kind, result=args.result))
            return
        report = (
            refresh_daily_snapshot(conn, root)
            if args.soak_command == "snapshot"
            else build_soak_report(conn, root, days=args.days)
        )
        conn.commit()
        if args.format == "markdown":
            print(render_soak_report_markdown(report), end="")
            return
        print_json(report)
        return


    if args.command in {"codex-sharing", "agi-sharing"}:
        if args.command == "agi-sharing":
            from .agi_bridge import owner_action
        else:
            from .codex_bridge import owner_action
        try:
            raw = sys.stdin.buffer.read(32769)
            if len(raw) > 32768:
                raise ValueError("payload_too_large")
            payload = json.loads(raw)
            print_json(owner_action(conn, args.action, payload))
        except (ValueError, OSError, sqlite3.Error):
            if conn.in_transaction:
                conn.rollback()
            print_json({"status": "rejected", "reason_code": "owner_action_invalid_stale_or_busy", "raw_access": False})
        return
    if args.command == "configure-screen-capture":
        print_json(configure_screen_capture(conn, enabled=args.enabled == "on"))
        return

    if args.command == "configure-hygiene-cleanup":
        print_json(
            configure_hygiene_cleanup(
                conn,
                Path(args.db).parent,
                auto_cleanup=None if args.auto is None else args.auto == "on",
                backup_rotation=None if args.backup_rotation is None else args.backup_rotation == "on",
                keep_backups=args.keep_backups,
                min_age_days=args.min_age_days,
                quarantine_days=args.quarantine_days,
            )
        )
        return

    if args.command == "configure-privacy-profile":
        try:
            print_json(configure_privacy_profile(conn, Path(args.db).parent, profile=args.profile))
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        return

    if args.command == "privacy-policy":
        if args.privacy_policy_command == "status":
            print_json(privacy_policy_status(conn, Path(args.db).parent))
            return
        if args.privacy_policy_command == "profile-preview":
            print_json(privacy_profile_preview(conn, Path(args.db).parent, profile=args.profile))
            return
        print_json(
            privacy_policy_preview(
                conn,
                app=args.app,
                domain=args.domain,
                title=args.title,
                text=args.text,
            )
        )
        return

    if args.command == "diagnostics":
        mode = "quick" if args.quick else args.mode
        payload = _run_busy_status_command(
            conn,
            lambda: diagnostics_status(conn, Path(args.db).parent, args.db, mode=mode),
            _diagnostics_busy_status,
        )
        if args.format == "markdown":
            markdown = render_diagnostics_markdown(payload)
            if args.out:
                output_path = Path(args.out).expanduser()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(markdown, encoding="utf-8")
                output_path.chmod(0o600)
                print_json(
                    {
                        "status": payload["status"],
                        "kind": payload["kind"],
                        "markdown_path": str(output_path.resolve()),
                        "raw_access": False,
                        "destructive_actions": False,
                        "memory_write_actions": False,
                        "cloud_analysis": "off",
                    }
                )
                return
            print(markdown, end="")
            return
        print_json(payload)
        return

    if args.command == "ai-connector":
        if args.ai_connector_command == "status":
            print_json(ai_connector_status(conn, Path(args.db).parent, host=args.host, port=args.port))
            return
        if args.ai_connector_command == "start":
            conn.close()
            print_json(start_ai_connector(args.db, Path(args.db).parent, host=args.host, port=args.port))
            return
        if args.ai_connector_command == "stop":
            conn.close()
            print_json(stop_ai_connector(Path(args.db).parent, host=args.host, port=args.port))
            return
        if args.ai_connector_command == "package":
            package = build_ai_connector_package(
                conn,
                range_name=args.range_name,
                max_items=args.max_items,
            )
            if args.format == "markdown":
                print(render_ai_handoff_markdown(package), end="")
                return
            print_json(package)
            return
        conn.close()
        serve_ai_connector(args.db, host=args.host, port=args.port)
        return

    if args.command == "configure-ha-automation":
        print_json(
            configure_ha_automation(
                conn,
                Path(args.db).parent,
                enabled=None if args.auto is None else args.auto == "on",
                interval_minutes=args.interval_minutes,
                trigger_mode=args.trigger_mode,
                hermes_profile=args.hermes_profile,
            )
        )
        return

    if args.command == "run-ha-automation-once":
        print_json(
            run_ha_automation_once(
                conn,
                Path(args.db).parent,
                db_path=args.db,
                force=args.force,
                trigger=not args.no_trigger,
            )
        )
        return

    if args.command == "ha-automation-status":
        print_json(
            _run_busy_status_command(
                conn,
                lambda: ha_automation_status(conn, Path(args.db).parent),
                _ha_automation_busy_status,
            )
        )
        return

    if args.command == "validate-ha-automation":
        print_json(validate_ha_automation(conn, Path(args.db).parent))
        return

    if args.command == "configure-quality-advice":
        try:
            print_json(
                configure_quality_advice(
                    conn,
                    Path(args.db).parent,
                    enabled=None if args.auto is None else args.auto == "on",
                    profile=args.profile,
                    interval_hours=args.interval_hours,
                )
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        return

    if args.command == "quality-advice":
        if args.quality_advice_command == "run":
            print_json(
                run_quality_advice_once(
                    conn,
                    Path(args.db).parent,
                    db_path=args.db,
                    start=args.start,
                    end=args.end,
                    force=args.force,
                    trigger=not args.no_trigger,
                )
            )
            return
        if args.quality_advice_command == "validate":
            print_json(validate_quality_advice(conn, Path(args.db).parent))
            return
        print_json(
            _run_busy_status_command(
                conn,
                lambda: quality_advice_status(conn, Path(args.db).parent),
                _quality_advice_busy_status,
            )
        )
        return

    if args.command == "quality-report":
        report = quality_report(conn, args.start, args.end)
        if args.format == "markdown":
            markdown = render_quality_markdown(report)
            if args.out:
                output_path = Path(args.out).expanduser()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(markdown, encoding="utf-8")
                output_path.chmod(0o600)
                print_json({"markdown_path": str(output_path.resolve()), "status": report["status"], "raw_access": False})
                return
            print(markdown, end="")
            return
        if args.out:
            output_path = Path(args.out).expanduser()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            output_path.chmod(0o600)
            print_json({"json_path": str(output_path.resolve()), "status": report["status"], "raw_access": False})
            return
        print_json(report)
        return

    if args.command == "memory-audit":
        report = memory_audit(args.start, args.end, limit=args.limit)
        if args.format == "markdown":
            markdown = render_memory_audit_markdown(report)
            if args.out:
                output_path = Path(args.out).expanduser()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(markdown, encoding="utf-8")
                output_path.chmod(0o600)
                print_json({"markdown_path": str(output_path.resolve()), "status": report["status"], "raw_access": False})
                return
            print(markdown, end="")
            return
        if args.out:
            output_path = Path(args.out).expanduser()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            output_path.chmod(0o600)
            print_json({"json_path": str(output_path.resolve()), "status": report["status"], "raw_access": False})
            return
        print_json(report)
        return

    if args.command == "memory-ledger":
        if args.memory_ledger_command == "refresh":
            print_json(refresh_memory_ledger(conn, ledger_path=args.path, detail_path=args.detail_path, limit=args.limit))
            return
        if args.memory_ledger_command == "sync":
            print_json(sync_memory_ledger(conn, ledger_path=args.path, detail_path=args.detail_path, backup_dir=args.backup_dir, dry_run=args.dry_run))
            return
        print_json(memory_ledger_status(conn, ledger_path=args.path, detail_path=args.detail_path))
        return

    if args.command == "memory-approval":
        if args.memory_approval_command == "status":
            print_json(memory_approval_status(conn, package_id=args.package, limit=args.limit))
            return
        if args.memory_approval_command == "candidates":
            print_json(memory_approval_candidates(conn, package_id=args.package, limit=args.limit))
            return
        if args.memory_approval_command == "preview":
            print_json(
                preview_memory_approval(
                    conn,
                    action=args.action,
                    ledger_id=args.ledger_id,
                    replacement_text=args.replacement_text,
                    package_id=args.package,
                    candidate_key=args.candidate_key,
                    memory_text=args.memory_text,
                )
            )
            return
        if args.memory_approval_command == "apply":
            print_json(apply_memory_approval(conn, plan_id=args.plan_id))
            return
        print_json(memory_approval_records(limit=args.limit))
        return

    if args.command == "memory-review":
        queue_path = Path(args.queue_path).expanduser() if getattr(args, "queue_path", None) else None
        ledger_path = Path(args.ledger_path).expanduser() if getattr(args, "ledger_path", None) else None
        nominations_path = (
            Path(args.nominations_path).expanduser() if getattr(args, "nominations_path", None) else None
        )
        try:
            if args.memory_review_command == "status":
                print_json(memory_review_status(
                    queue_path=queue_path, ledger_path=ledger_path, nominations_path=nominations_path, display_root=Path(args.db).parent
                ))
                return
            if args.memory_review_command == "list":
                print_json(
                    memory_review_candidates(
                        limit=args.limit,
                        state=args.state,
                        queue_path=queue_path,
                        ledger_path=ledger_path,
                        include_nominations=not args.no_nominations,
                        nominations_path=nominations_path,
                        display_root=Path(args.db).parent,
                    )
                )
                return
            if args.memory_review_command == "nominate":
                from .presentation_scope import DisplayScope
                if DisplayScope(Path(args.db).parent).hidden_memory(args.memory_id):
                    raise MemoryReviewError("source_not_in_display_scope")
                print_json(
                    nominate_memory(
                        args.memory_id,
                        action=args.action,
                        note=args.note,
                        superseded_by=args.superseded_by,
                        evidence_text=args.evidence_text,
                        nominations_path=nominations_path,
                        queue_path=queue_path,
                        ledger_path=ledger_path,
                    )
                )
                return
            if args.memory_review_command == "reveal":
                from .presentation_scope import DisplayScope
                scope = DisplayScope(Path(args.db).parent)
                if scope.hidden_memory(args.memory_id):
                    print_json({"interface": "memory-review", "ok": False, "error": "source_not_in_display_scope", "raw_access": False})
                    return
                chroma_path = Path(args.chroma_path).expanduser() if args.chroma_path else None
                result = read_memory_text(args.memory_id, chroma_path=chroma_path,
                    admission_registry=scope.memory_sources if scope.enabled else None)
                print_json(scope.project(result))
                return
            decisions = json.loads(args.decisions)
            if not isinstance(decisions, list):
                raise MemoryReviewError("--decisions must be a JSON array")
            from .presentation_scope import DisplayScope
            display_scope = DisplayScope(Path(args.db).parent)
            if any(display_scope.hidden_memory(row.get("memory_id")) for row in decisions):
                raise MemoryReviewError("source_not_in_display_scope")
            if args.memory_review_command == "preview":
                print_json(
                    preview_review_batch(
                        decisions, queue_path=queue_path, ledger_path=ledger_path,
                        include_nominations=True, nominations_path=nominations_path,
                    )
                )
                return
            print_json(
                apply_review_batch(
                    decisions,
                    confirm_digest=args.confirm_digest,
                    queue_path=queue_path,
                    ledger_path=ledger_path,
                    include_nominations=True,
                    nominations_path=nominations_path,
                )
            )
            return
        except json.JSONDecodeError:
            print_json({"interface": "memory-review", "ok": False, "error": "decisions_not_json"})
            return
        except (MemoryReviewError, ValueError, TypeError) as exc:
            print_json({"interface": "memory-review", "ok": False, "error": str(exc)})
            return

    if args.command == "decision-queue":
        if args.decision_queue_command == "list":
            print_json(
                decision_queue_list(
                    conn,
                    range_name=args.range_name,
                    start=args.start,
                    end=args.end,
                    package_id=args.package,
                    limit=args.limit,
                    include_decided=args.include_decided,
                )
            )
            return
        if args.decision_queue_command == "record":
            print_json(
                record_decision_queue_action(
                    conn,
                    item_key=args.item_key,
                    action=args.action,
                    note=args.note,
                    generalize_scope=args.generalize,
                    range_name=args.range_name,
                    start=args.start,
                    end=args.end,
                    package_id=args.package,
                )
            )
            return
        if args.decision_queue_command == "preview-write":
            print_json(
                preview_decision_queue_write(
                    conn,
                    item_key=args.item_key,
                    memory_text=args.memory_text,
                    range_name=args.range_name,
                    start=args.start,
                    end=args.end,
                    package_id=args.package,
                )
            )
            return
        print_json(apply_decision_queue_plan(conn, plan_id=args.plan_id))
        return

    if args.command == "activity-confirmation":
        if args.activity_confirmation_command == "list":
            print_json(
                list_activity_confirmations(
                    conn,
                    range_name=args.range_name,
                    start=args.start,
                    end=args.end,
                    limit=args.limit,
                    include_resolved=args.include_resolved,
                )
            )
            return
        if args.activity_confirmation_command == "cards":
            print_json(
                list_activity_confirmation_cards(
                    conn,
                    range_name=args.range_name,
                    start=args.start,
                    end=args.end,
                    limit=args.limit,
                    cursor=args.cursor,
                    section=args.section,
                )
            )
            return
        if args.activity_confirmation_command == "revision":
            print_json(
                activity_confirmation_revision(
                    conn,
                    range_name=args.range_name,
                    start=args.start,
                    end=args.end,
                )
            )
            return
        if args.activity_confirmation_command == "revalidate":
            print_json(
                revalidate_unresolved_confirmation_policy(
                    conn,
                    dry_run=not args.apply,
                )
            )
            return
        if args.activity_confirmation_command == "answer":
            print_json(
                answer_activity_confirmation(
                    conn,
                    item_key=args.item_key,
                    answer=args.answer,
                    reason=args.reason,
                    note=_activity_confirmation_note(args),
                    generalize_scope=args.generalize,
                )
            )
            return
        if args.activity_confirmation_command == "answer-group":
            details = _activity_confirmation_details(args)
            print_json(
                answer_activity_confirmation_group(
                    conn,
                    group_key=args.group_key,
                    member_revision=args.member_revision,
                    answer=args.answer,
                    reason=args.reason,
                    note=str(details.get("note") or "") if details else _activity_confirmation_note(args),
                    workstream=str(details.get("workstream") or "") if details else "",
                    outcome=str(details.get("outcome") or "") if details else "",
                    generalize_scope=args.generalize,
                )
            )
            return
        if args.activity_confirmation_command == "clarify":
            print_json(
                clarify_activity_confirmation_group(
                    conn,
                    group_key=args.group_key,
                    member_revision=args.member_revision,
                    note=_activity_confirmation_note(args),
                )
            )
            return
        if args.activity_confirmation_command == "configure":
            print_json(configure_activity_confirmations(conn, mode=args.mode))
            return
        if args.activity_confirmation_command == "manual-add":
            payload = _activity_confirmation_json_stdin(
                max_bytes=8192,
                allowed_keys={"safe_label", "workstream", "outcome", "started_at", "ended_at"},
            )
            print_json(
                manual_add_confirmed_activity(
                    conn,
                    confirmation_action=args.confirm,
                    safe_label=str(payload.get("safe_label") or ""),
                    workstream=str(payload.get("workstream") or ""),
                    outcome=str(payload.get("outcome") or "unknown"),
                    started_at=str(payload.get("started_at") or ""),
                    ended_at=str(payload.get("ended_at") or "") or None,
                )
            )
            return
        if args.activity_confirmation_command == "settle":
            print_json(settle_activity_confirmations(conn, report_date=args.date))
            return
        print_json(
            revoke_confirmation_calibration(
                conn,
                calibration_key=args.calibration_key,
            )
        )
        return

    if args.command == "quality-feedback":
        if args.quality_feedback_command == "add":
            try:
                result = add_quality_feedback(
                    conn,
                    target_type=args.target_type,
                    target_id=args.target_id,
                    issue_type=args.issue_type,
                    expected_status=args.expected,
                    note=args.note,
                )
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            print_json(result)
            return
        print_json(list_quality_feedback(conn, args.start, args.end))
        return



    if args.command == "pause":
        if args.manual:
            print_json(pause(conn, mode="manual"))
        elif args.until_lock:
            print_json(pause(conn, mode="until_lock"))
        elif args.today:
            print_json(pause(conn, mode="today"))
        else:
            print_json(pause(conn, args.minutes))
        return

    if args.command == "resume":
        print_json(resume(conn))
        return

    if args.command == "cleanup-retention":
        if args.days is not None and args.days <= 0:
            raise SystemExit("cleanup-retention --days must be at least 1")
        if args.apply:
            if args.confirm_retention_delete != "DELETE_EXPIRED_LOCAL_ACTIVITY":
                raise SystemExit(
                    "cleanup-retention --apply requires --confirm-retention-delete DELETE_EXPIRED_LOCAL_ACTIVITY"
                )
            print_json(cleanup_retention(conn, Path(args.db).parent, args.days)["summary"])
        else:
            print_json(cleanup_retention_preview(conn, Path(args.db).parent, args.days))
        return

    if args.command == "reclassify":
        print_json(reclassify_existing(conn, args.start, args.end, dry_run=args.dry_run))
        return

    if args.command == "document-signal-collisions":
        try:
            if args.mode == "apply":
                result = apply_document_signal_identity_collisions(
                    conn,
                    confirm_hash=args.confirm_hash,
                    historical_cutoff=args.historical_cutoff,
                )
            elif args.mode == "reverse":
                result = reverse_document_signal_identity_collisions(
                    conn,
                    manifest_hash=args.manifest_hash,
                )
            else:
                result = preview_document_signal_identity_collisions(
                    conn,
                    historical_cutoff=args.historical_cutoff,
                )
        except (RuntimeError, ValueError) as exc:
            result = {
                "interface": "document_signal_identity_collisions",
                "status": "refused",
                "safe_status": str(exc),
                "candidate_count": 0,
                "updated_count": 0,
                "restored_count": 0,
                "manifest_hash": "",
                "deleted_count": 0,
                "attribution_changed_count": 0,
                "raw_access": False,
                "destructive_actions": False,
            }
        print_json(result)
        return

    if args.command == "build-helpers":
        print_json({"helpers": ensure_all_helpers(Path(args.db).parent)})
        return

    if args.command == "install-browser-hosts":
        print_json(install_native_host_manifests(Path(__file__).resolve().parents[1], args.browser, args.extension_id))
        return

    if args.command == "browser-native-host":
        conn.close()
        native_host_loop(Path(args.core_root).expanduser() if args.core_root else native_host_core_root())
        return

    if args.command == "install-launch-agent":
        conn.close()
        print_json(
            install_launch_agent(
                args.db,
                privacy_profile=args.privacy_profile,
                preserve_privacy_profile=args.preserve_privacy_profile,
            )
        )
        return

    if args.command == "uninstall-launch-agent":
        configure_screen_capture(conn, enabled=False)
        conn.close()
        print_json(uninstall_launch_agent())
        return

    if args.command == "serve":
        conn.close()
        serve(args.db, args.host, args.port)


def _connect_for_command(args: argparse.Namespace):
    if not _read_only_command(args):
        if _status_busy_command(args):
            busy_timeout_ms = _status_connection_busy_timeout_ms(args)
            return ensure_ready(args.db, busy_timeout_seconds=busy_timeout_ms / 1000)
        return ensure_ready(args.db)
    path = Path(args.db).expanduser()
    if path.exists():
        if _phase2_context_read_command(args):
            return connect_readonly(
                args.db,
                busy_timeout_seconds=QUICK_STATUS_BUSY_TIMEOUT_MS / 1000,
            )
        if _quick_status_command(args):
            return connect_readonly(
                args.db,
                busy_timeout_seconds=QUICK_STATUS_BUSY_TIMEOUT_MS / 1000,
            )
        return connect_readonly(args.db)
    if args.command == "activity-confirmation" and getattr(
        args, "activity_confirmation_command", ""
    ) in {"list", "cards", "revision"}:
        raise FileNotFoundError("configured database is missing")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _activity_confirmation_note(args: argparse.Namespace) -> str:
    """Read a private note without requiring it to appear in process argv."""
    if not bool(getattr(args, "note_stdin", False)):
        return str(getattr(args, "note", "") or "")
    value = sys.stdin.read(4097)
    if len(value) > 4096:
        raise ValueError("activity confirmation note exceeds stdin limit")
    return value


def _activity_confirmation_details(args: argparse.Namespace) -> dict[str, str]:
    if not bool(getattr(args, "details_stdin", False)):
        return {}
    payload = _activity_confirmation_json_stdin(
        max_bytes=4096,
        allowed_keys={"note", "workstream", "outcome"},
    )
    return {key: str(value or "") for key, value in payload.items()}


def _activity_confirmation_json_stdin(
    *,
    max_bytes: int,
    allowed_keys: set[str],
) -> dict[str, object]:
    value = sys.stdin.read(max_bytes + 1)
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError("activity confirmation JSON exceeds stdin limit")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid activity confirmation JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("activity confirmation JSON must be an object")
    unknown = set(payload) - allowed_keys
    if unknown:
        raise ValueError("unsupported activity confirmation JSON fields")
    return payload


def _context_query_stdin(args: argparse.Namespace) -> str:
    if not bool(getattr(args, "query_stdin", False)):
        return ""
    payload = _activity_confirmation_json_stdin(
        max_bytes=1024,
        allowed_keys={"query"},
    )
    value = payload.get("query", "")
    if not isinstance(value, str):
        raise ValueError("context query must be text")
    try:
        query = _validate_search_query(value)
    except ContractError as exc:
        raise ValueError(exc.reason_code) from exc
    if not query:
        raise ValueError("context query must not be empty")
    return query


def _context_secret_from_env(name: str) -> bytes:
    value = os.environ.get(str(name or ""), "")
    if not value:
        raise SystemExit(f"context-console requires private key material in environment variable {name}")
    return value.encode("utf-8")


_DISPLAY_ROOT = None


def print_json(payload) -> None:
    if _DISPLAY_ROOT is not None:
        from .presentation_scope import project_for_runtime
        payload = project_for_runtime(payload, _DISPLAY_ROOT)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _run_timeline_command(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Build and emit one CLI timeline under a single authorization lease."""
    with document_authorization_lock(conn, exclusive=False):
        activities = list(confirmed_activity_view(conn, args.start, args.end).get("activities") or [])
        if args.format in {"markdown", "learning-markdown"}:
            export = cleaned_timeline_export(conn, args.start, args.end)
            learning = args.format == "learning-markdown"
            markdown = render_learning_timeline_markdown(export) if learning else render_cleaned_timeline_markdown(export)
            if args.out:
                output_path = write_timeline_markdown(args.out, markdown)
                learning_digest = export.get("learning_digest", {})
                learnability = learning_digest.get("learnability_counts", {}) if isinstance(learning_digest, dict) else {}
                print_json(
                    {
                        "markdown_path": str(output_path),
                        "activity_count": len(export["activities"]),
                        "duplicate_event_count": len(export.get("duplicate_events", [])),
                        "privacy_event_count": len(export["privacy_events"]),
                        "gap_event_count": len(export["gap_events"]),
                        "total_event_count": len(export["items"]),
                        "learning_digest": {
                            "useful_count": int(learnability.get("useful", 0)),
                            "low_signal_count": int(learnability.get("low_signal", 0)),
                            "protected_count": int(learnability.get("protected", 0)),
                            "duplicate_count": int(learnability.get("duplicate", 0)),
                            "gap_count": int(learnability.get("gap", 0)),
                        },
                        "source": "local_learning_timeline" if learning else "local_cleaned_timeline",
                        "raw_access": False,
                        "cloud_analysis": "off",
                    }
                )
                return
            print(markdown, end="")
            return
        if args.out:
            output_path = Path(args.out).expanduser()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(activities, ensure_ascii=False, indent=2), encoding="utf-8")
            output_path.chmod(0o600)
            print_json({"json_path": str(output_path.resolve()), "activity_count": len(activities)})
            return
        print_json(activities)


def _run_get_package_command(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Read and emit one HA package under one transport authorization lease."""
    with document_authorization_lock(conn, exclusive=False):
        if args.memory_candidates_only:
            payload = get_package_memory_candidates(conn, args.package_id, max_items=args.max_items)
        elif args.handoff_summary:
            payload = get_package_handoff_summary(conn, args.package_id, max_items=args.max_items)
        else:
            payload = get_package(conn, args.package_id)
        print_json(payload)


def _run_list_packages_command(conn: sqlite3.Connection) -> None:
    """List and emit HA packages under one transport authorization lease."""
    with document_authorization_lock(conn, exclusive=False):
        print_json(list_packages(conn))


# Status and diagnostics must fail closed quickly when the database is busy.
# A longer wait here is especially harmful because it happens before the
# command-specific fallback can emit its truthful ``unknown`` envelope.
STATUS_COMMAND_BUSY_TIMEOUT_MS = 1000
QUICK_STATUS_BUSY_TIMEOUT_MS = 250


def _status_connection_busy_timeout_ms(args: argparse.Namespace) -> int:
    if _quick_status_command(args):
        return QUICK_STATUS_BUSY_TIMEOUT_MS
    if args.command == "diagnostics" and (
        bool(getattr(args, "quick", False))
        or getattr(args, "mode", "full") == "quick"
    ):
        return QUICK_STATUS_BUSY_TIMEOUT_MS
    return STATUS_COMMAND_BUSY_TIMEOUT_MS


def _run_busy_status_command(conn, build, fallback):
    conn.execute(f"PRAGMA busy_timeout = {STATUS_COMMAND_BUSY_TIMEOUT_MS}")
    try:
        return build()
    except sqlite3.OperationalError as exc:
        if _sqlite_busy(exc):
            return fallback()
        raise
    finally:
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")


def _run_quick_status_command(conn, build):
    conn.execute(f"PRAGMA busy_timeout = {QUICK_STATUS_BUSY_TIMEOUT_MS}")
    try:
        return build()
    except sqlite3.OperationalError as exc:
        if _sqlite_busy(exc):
            return _core_quick_status_busy_status()
        raise
    finally:
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")


def _status_busy_command(args: argparse.Namespace) -> bool:
    if args.command in {"status", "ha-automation-status", "diagnostics"}:
        return True
    return args.command == "quality-advice" and getattr(args, "quality_advice_command", "") == "status"


def _quick_status_command(args: argparse.Namespace) -> bool:
    return args.command == "status" and bool(getattr(args, "quick", False))


def _connect_busy_status(args: argparse.Namespace) -> dict:
    if args.command == "status":
        if getattr(args, "quick", False):
            return _core_quick_status_busy_status()
        return _core_status_busy_status()
    if args.command == "ha-automation-status":
        return _ha_automation_busy_status()
    if args.command == "diagnostics":
        return _diagnostics_busy_status()
    return _quality_advice_busy_status()


def _sqlite_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _ha_automation_busy_status() -> dict:
    return {
        "enabled": None,
        "status": "unknown",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_successful_read_at": None,
        "label": "暂时不可读",
        "kind": "warning",
        "summary": "HA 自动化状态暂时不可读：数据库正在被后台记录任务占用；稍后重试即可。",
        "last_error": "database_busy",
        "pending_count": None,
        "failed_count": None,
        "dead_letter_count": None,
        "unresolved_dead_letter_count": None,
        "memory_report_pending_count": None,
        "memory_report_failed_count": None,
        "raw_access": False,
    }


def _quality_advice_busy_status() -> dict:
    return {
        "enabled": None,
        "status": "unknown",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_successful_read_at": None,
        "label": "暂时不可读",
        "kind": "warning",
        "summary": "质量顾问状态暂时不可读：数据库正在被后台记录任务占用；稍后重试即可。",
        "last_error": "database_busy",
        "latest_recommendation_count": None,
        "latest_issue_count": None,
        "raw_access": False,
    }


def _core_status_busy_status() -> dict:
    """Return the fail-closed public envelope for an unreadable full status.

    A busy read is not a degraded success.  Deliberately omit every field that
    could be mistaken for a current service, health, count, session, document,
    or trusted-context value.
    """
    return {
        "status": "unknown",
        "reason_code": "database_busy",
        "kind": "warning",
        "interface": "full_status",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_successful_read_at": None,
        "capture_allowed_by_session": False,
        "actions_enabled": False,
        "raw_access": False,
    }


def _core_status_current_status(conn: sqlite3.Connection, root_dir: str | Path) -> dict:
    """Attach the Core-issued interface and observation time to a successful read."""
    payload = get_status(conn, root_dir)
    payload["interface"] = "full_status"
    payload["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return payload


def _core_quick_status_busy_status() -> dict:
    """Return the exact content-free quick-status busy contract."""
    return {
        "status": "unknown",
        "reason_code": "database_busy",
        "kind": "warning",
        "interface": "quick_status",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_successful_read_at": None,
        "attention_codes": ["database_busy"],
        "capture_allowed_by_session": False,
        "actions_enabled": False,
        "raw_access": False,
    }


def _core_quick_status_unavailable_status(reason: str) -> dict:
    safe_reason = reason if reason in {"database_missing", "database_unavailable", "database_incompatible"} else "database_unavailable"
    return {
        "status": "unavailable",
        "reason_code": safe_reason,
        "kind": "warning",
        "interface": "quick_status",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_successful_read_at": None,
        "attention_codes": [safe_reason],
        "capture_allowed_by_session": False,
        "actions_enabled": False,
        "raw_access": False,
    }


def _diagnostics_busy_status() -> dict:
    """Return a diagnostic warning without inventing service or health facts."""
    return {
        "status": "unknown",
        "reason_code": "database_busy",
        "kind": "warning",
        "interface": "diagnostics",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_successful_read_at": None,
        "capture_allowed_by_session": False,
        "actions_enabled": False,
        "summary": "数据库不可读，诊断状态未知；未沿用旧健康或服务快照。",
        "safety_boundary": {
            "raw_access": False,
            "destructive_actions": False,
            "memory_write_actions": False,
            "rule_write_actions": False,
            "cloud_analysis": "off",
            "reads_raw_sources": False,
        },
        "probes": [
            {
                "name": "database_busy",
                "label": "数据库读窗口",
                "status": "unknown",
                "kind": "warning",
                "summary": "后台记录正在写入数据库；当前服务、计数和健康状态均未判定。",
                "raw_access": False,
                "destructive_actions": False,
            }
        ],
        "recommendations": ["稍后刷新诊断中心；连续出现时再检查本机 daemon 日志。"],
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "rule_write_actions": False,
        "cloud_analysis": "off",
    }


def add_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path")


def _read_only_command(args: argparse.Namespace) -> bool:
    if args.command == "document-signal-collisions" and getattr(args, "mode", "dry-run") in {"dry-run", "preview"}:
        return True
    if args.command == "context-console" and getattr(args, "context_console_command", "") in {
        "status",
        "bundle",
        "experiment-status",
        "snapshot",
        "memory-candidates",
        "delivery-status",
    }:
        return True
    if args.command == "status" and getattr(args, "quick", False):
        return True
    if args.command == "quality-report":
        return True
    if args.command == "report-context":
        return True
    if args.command == "memory-audit":
        return True
    if args.command == "memory-ledger" and getattr(args, "memory_ledger_command", "") == "status":
        return True
    if args.command == "memory-approval" and getattr(args, "memory_approval_command", "") in {"status", "candidates", "records"}:
        return True
    if args.command == "memory-review" and getattr(args, "memory_review_command", "") in {
        "status",
        "list",
        "reveal",
        "preview",
        "nominate",
    }:
        return True
    if args.command == "decision-queue" and getattr(args, "decision_queue_command", "") == "list":
        return True
    if args.command == "activity-confirmation" and getattr(args, "activity_confirmation_command", "") in {
        "list",
        "cards",
        "revision",
    }:
        return True
    if args.command == "get-package":
        return True
    if args.command == "ai-handoff":
        return True
    if args.command == "document-changes" and getattr(args, "document_changes_command", "") in {"status", "list", "history"}:
        return True
    if args.command == "privacy-policy":
        return True
    if args.command == "ai-connector" and getattr(args, "ai_connector_command", "") in {"status", "package"}:
        return True
    return False


def _phase2_context_read_command(args: argparse.Namespace) -> bool:
    return args.command == "context-console" and getattr(
        args,
        "context_console_command",
        "",
    ) in {"snapshot", "memory-candidates", "delivery-status", "experiment-status"}


def _phase2_context_contract_version(args: argparse.Namespace) -> str:
    return {
        "snapshot": CONTEXT_SNAPSHOT_VERSION,
        "memory-candidates": MEMORY_CANDIDATE_LIST_VERSION,
        "delivery-status": DELIVERY_STATUS_VERSION,
        "experiment-status": PRODUCT_EXPERIMENT_STATUS_VERSION,
    }[str(getattr(args, "context_console_command", ""))]


def _safe_threading_excepthook(args: threading.ExceptHookArgs) -> None:
    """Emit a content-free record for uncaught background-thread failures."""

    if args.exc_type is SystemExit:
        return
    error = (
        "database_busy"
        if isinstance(args.exc_value, sqlite3.OperationalError) and _sqlite_busy(args.exc_value)
        else "thread_failed"
    )
    payload = {"status": "error", "error": error, "raw_access": False}
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


def _safe_cli_entrypoint(entrypoint=main) -> None:
    """Run the CLI without writing exception details or local paths to logs."""

    previous_threading_excepthook = threading.excepthook
    threading.excepthook = _safe_threading_excepthook
    try:
        entrypoint()
    except Exception as exc:
        if isinstance(exc, FileNotFoundError):
            error = "database_missing"
        else:
            error = "database_busy" if isinstance(exc, sqlite3.OperationalError) and _sqlite_busy(exc) else "command_failed"
        payload = {"status": "error", "error": error, "raw_access": False}
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    finally:
        threading.excepthook = previous_threading_excepthook


if __name__ == "__main__":
    _safe_cli_entrypoint()
