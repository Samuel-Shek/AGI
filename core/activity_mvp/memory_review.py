"""Second front end over the shared long-term memory review backlog.

The review queue and the decision ledger are produced and consumed outside
PAM: a governance audit writes the queue, and Mem0 retrieval reads the ledger
to decide whether a queued memory may enter an AI prompt. This module lets PAM
host the review without a separate local web service, writing the exact same
ledger tables so the existing retrieval policy keeps working unchanged.

Reads are query-only. Nothing here deletes a memory, and a decision is only
written by ``apply_review_batch`` after a preview digest is confirmed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_QUEUE_PATH = Path(
    "~/.hermes/obsidian-command-rules/analysis/memory-quality/review-queue-v3.json"
).expanduser()
DEFAULT_LEDGER_PATH = Path(
    "~/.hermes/memory-review-center/state/review-ledger.sqlite3"
).expanduser()
DEFAULT_CHROMA_PATH = Path("/Volumes/mem0/chroma/chroma.sqlite3")
DEFAULT_COLLECTION_NAME = "mem0"
OWNER_ID = "local-owner"
REVIEWER = "pam-app"

# ``supersede`` is deliberately absent: it archives the old memory *and* writes
# a replacement into Mem0, and PAM's controlled memory writer stays disabled.
ACTIONS = (
    "keep_active",
    "review_later",
    "mark_legacy",
    "mark_expired",
    "quarantine_wrong_subject",
)
LIFECYCLE_BY_ACTION = {
    "keep_active": "active",
    "review_later": "review_required",
    "mark_legacy": "legacy",
    "mark_expired": "expired",
    "quarantine_wrong_subject": "quarantined",
    "supersede": "superseded",
}
ACTION_TITLES = {
    "keep_active": "继续有效",
    "review_later": "稍后复核",
    "mark_legacy": "标为历史",
    "mark_expired": "标为过期",
    "quarantine_wrong_subject": "隔离（主体不符）",
    "supersede": "已被替代",
}
MAX_QUEUE_BYTES = 8 * 1024 * 1024
MAX_BATCH_ITEMS = 500
QUEUE_SCHEMA_VERSION = 3
REVIEW_POLICY_VERSION = "2026-08-26.1"
MAX_QUEUE_AGE = timedelta(hours=72)
SHA256_HEX = frozenset("0123456789abcdef")


class MemoryReviewError(RuntimeError):
    """A review artifact is missing, malformed, or refuses the request."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: Any, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryReviewError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise MemoryReviewError(f"{label} needs a timezone")
    return parsed.astimezone(timezone.utc)


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(character in SHA256_HEX for character in text)


def _candidate_fingerprint(
    *,
    policy_version: str,
    memory_id: str,
    content_sha256: str,
    categories: list[str],
    related_memory_ids: list[str],
) -> str:
    payload = {
        "policy_version": policy_version,
        "memory_id": memory_id,
        "content_sha256": content_sha256,
        "categories": sorted(set(categories)),
        "related_memory_ids": sorted(set(related_memory_ids)),
    }
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _bounded_regular_file(path: Path, *, max_bytes: int) -> bytes:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError as exc:
        raise MemoryReviewError(f"review artifact is missing: {path.name}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
        raise MemoryReviewError(f"review artifact is not a bounded regular file: {path.name}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        return os.read(descriptor, max_bytes)
    finally:
        os.close(descriptor)


def read_queue(
    queue_path: Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Copy the audit-produced queue's public identity contract."""
    path = Path(queue_path) if queue_path else DEFAULT_QUEUE_PATH
    raw = _bounded_regular_file(path, max_bytes=MAX_QUEUE_BYTES)
    try:
        source = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MemoryReviewError("review queue must be UTF-8 JSON") from exc
    if not isinstance(source, dict) or source.get("schema_version") != QUEUE_SCHEMA_VERSION:
        raise MemoryReviewError("unsupported review queue schema")
    if source.get("policy_version") != REVIEW_POLICY_VERSION:
        raise MemoryReviewError("review queue policy version does not match PAM")
    if source.get("owner_id") != OWNER_ID:
        raise MemoryReviewError("review queue owner does not match this machine's owner")
    generated_at = _parse_time(source.get("generated_at"), label="review queue generated_at")
    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = observed_now - generated_at
    if age < timedelta(minutes=-5):
        raise MemoryReviewError("review queue is from the future")
    if age > MAX_QUEUE_AGE:
        raise MemoryReviewError("review queue is stale")
    if not source.get("audit_watermark"):
        raise MemoryReviewError("review queue audit watermark is missing")
    if not _is_sha256(source.get("source_snapshot_sha256")):
        raise MemoryReviewError("review queue source snapshot hash is invalid")
    items_source = source.get("items")
    if not isinstance(items_source, list):
        raise MemoryReviewError("review queue items must be a list")
    if source.get("unique_count") != len(items_source):
        raise MemoryReviewError("review queue unique count does not match its items")
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in items_source:
        if not isinstance(row, dict):
            raise MemoryReviewError("review queue item must be an object")
        memory_id = str(row.get("memory_id") or "").strip()
        content_sha256 = str(row.get("content_sha256") or "").strip().lower()
        fingerprint = str(row.get("candidate_fingerprint") or "").strip().lower()
        categories = row.get("categories")
        reason_codes = row.get("reason_codes")
        related_ids = row.get("related_memory_ids")
        if not memory_id or memory_id in seen_ids or not _is_sha256(content_sha256):
            raise MemoryReviewError("review queue item requires memory_id and content_sha256")
        if not isinstance(categories, list) or not all(
            isinstance(value, str) and value for value in categories
        ):
            raise MemoryReviewError("review queue item categories are invalid")
        if not isinstance(reason_codes, list) or not all(
            isinstance(value, str) and value for value in reason_codes
        ):
            raise MemoryReviewError("review queue item reason codes are invalid")
        if not isinstance(related_ids, list) or not all(
            isinstance(value, str) and value for value in related_ids
        ):
            raise MemoryReviewError("review queue related IDs are invalid")
        expected_fingerprint = _candidate_fingerprint(
            policy_version=REVIEW_POLICY_VERSION,
            memory_id=memory_id,
            content_sha256=content_sha256,
            categories=categories,
            related_memory_ids=related_ids,
        )
        if not _is_sha256(fingerprint) or fingerprint != expected_fingerprint:
            raise MemoryReviewError("review queue candidate fingerprint is invalid")
        seen_ids.add(memory_id)
        items.append(
            {
                "memory_id": memory_id,
                "content_sha256": content_sha256,
                "candidate_fingerprint": fingerprint,
                "policy_version": REVIEW_POLICY_VERSION,
                "categories": [str(value) for value in categories],
                "reason_codes": [str(value) for value in reason_codes],
                "related_memory_ids": [str(value) for value in related_ids],
                "rule_ids": [str(value) for value in row.get("rule_ids") or []],
                "evidence_types": [str(value) for value in row.get("evidence_types") or []],
                "last_seen_at": str(row.get("last_seen_at") or ""),
            }
        )
    return {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "policy_version": REVIEW_POLICY_VERSION,
        "generated_at": generated_at.isoformat(),
        "queue_age_hours": max(age.total_seconds() / 3600, 0.0),
        "audit_watermark": source.get("audit_watermark"),
        "source_snapshot_sha256": str(source.get("source_snapshot_sha256")),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "item_count": len(items),
        "items": items,
    }


def _connect_read_only(path: Path, *, timeout: int = 2) -> sqlite3.Connection:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError as exc:
        raise MemoryReviewError(f"database is unavailable: {path.name}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise MemoryReviewError(f"database must be a regular file: {path.name}")
    connection = sqlite3.connect(
        f"file:{path}?mode=ro", uri=True, timeout=timeout, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def read_decisions(ledger_path: Path | None = None) -> dict[tuple[str, str], dict[str, str]]:
    """Return the applied lifecycle overrides keyed by memory identity."""
    path = Path(ledger_path) if ledger_path else DEFAULT_LEDGER_PATH
    if not path.exists():
        return {}
    connection = _connect_read_only(path)
    try:
        rows = connection.execute(
            """
            SELECT memory_id, content_sha256, lifecycle_status, decision, applied_at,
                   candidate_fingerprint, policy_version
            FROM lifecycle_overrides
            WHERE applied_at IS NOT NULL
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise MemoryReviewError("decision ledger read failed") from exc
    finally:
        connection.close()
    return {
        (row["memory_id"], row["content_sha256"]): {
            "lifecycle_status": row["lifecycle_status"],
            "decision": row["decision"],
            "applied_at": row["applied_at"],
            "candidate_fingerprint": row["candidate_fingerprint"] or "",
            "policy_version": row["policy_version"] or "",
        }
        for row in rows
    }


def _admission_metadata(connection, memory_id, collection_name):
    return {row['key']: next((row[k] for k in ('string_value', 'int_value', 'float_value', 'bool_value') if row[k] is not None), None)
        for row in connection.execute(
            "SELECT m.key,m.string_value,m.int_value,m.float_value,m.bool_value FROM embeddings e JOIN segments s ON s.id=e.segment_id JOIN collections c ON c.id=s.collection JOIN embedding_metadata m ON m.id=e.id WHERE e.embedding_id=? AND c.name=? AND m.key IN ('user_id','owner_id','memory_user_id','schema_version','applies_to','source_id','path','cwd','root','source_path','project_path','mixed')",
            (memory_id, collection_name))}


def read_memory_metadata(memory_id, *, chroma_path=None):
    """Ownership and source fields only; the memory body is never selected."""
    connection = _connect_read_only(Path(chroma_path) if chroma_path else DEFAULT_CHROMA_PATH)
    try:
        return _admission_metadata(connection, memory_id, DEFAULT_COLLECTION_NAME)
    except sqlite3.Error as exc:
        raise MemoryReviewError('memory metadata read failed') from exc
    finally:
        connection.close()


def read_memory_text(
    memory_id: str,
    *,
    chroma_path: Path | None = None,
    collection_name: str = DEFAULT_COLLECTION_NAME,
    admission_registry: dict | None = None,
) -> dict[str, Any]:
    """Reveal one memory's text on demand through the read-only store.

    The queue and candidate list never carry memory text. Reveal is a separate,
    explicit call so a listing cannot leak long-term memory content wholesale.
    """
    path = Path(chroma_path) if chroma_path else DEFAULT_CHROMA_PATH
    connection = _connect_read_only(path)
    try:
        connection.execute('BEGIN')
        if admission_registry is not None:
            import sys
            from activity_mvp.source_governance import personal_sources
            metadata = _admission_metadata(connection, memory_id, collection_name)
            if not personal_sources.memory_metadata_admitted(memory_id,metadata,admission_registry):raise MemoryReviewError('owner_retired_or_source_unknown')
        rows = connection.execute(
            """
            SELECT m.key, m.string_value, m.int_value, m.float_value, m.bool_value
            FROM embeddings e
            JOIN segments s ON s.id = e.segment_id
            JOIN collections c ON c.id = s.collection
            JOIN embedding_metadata m ON m.id = e.id
            WHERE e.embedding_id = ? AND c.name = ?
            """,
            (memory_id, collection_name),
        ).fetchall()
    except sqlite3.Error as exc:
        raise MemoryReviewError("memory store read failed") from exc
    finally:
        connection.close()
    if not rows:
        raise MemoryReviewError("memory is not present in the scoped collection")
    values: dict[str, Any] = {}
    for row in rows:
        value = (
            row["string_value"]
            if row["string_value"] is not None
            else row["int_value"]
            if row["int_value"] is not None
            else row["float_value"]
            if row["float_value"] is not None
            else row["bool_value"]
        )
        values[str(row["key"])] = value
    owner_fields = {
        key: str(values.get(key) or "").strip()
        for key in ("user_id", "owner_id", "memory_user_id")
    }
    present_owners = {value for value in owner_fields.values() if value}
    if any(value != OWNER_ID for value in present_owners):
        raise MemoryReviewError("memory owner fields conflict with this machine's owner")
    schema_version = str(values.get("schema_version") or "").strip()
    if schema_version == "3" and any(value != OWNER_ID for value in owner_fields.values()):
        raise MemoryReviewError("schema v3 memory requires complete owner fields")
    text = str(values.get("data") or values.get("memory") or "")
    return {
        "memory_id": memory_id,
        "text": text,
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "schema_version": schema_version,
        "observed_at": values.get("observed_at") or "",
        "applies_to": values.get("applies_to") or "",
        "status": values.get("status") or "",
        "review_status": values.get("review_status") or "",
    }


def _load_nominations(nominations_path: Path | None) -> dict[str, dict[str, Any]]:
    from . import memory_nominations

    return memory_nominations.nomination_items(nominations_path)


def memory_review_candidates(
    *,
    limit: int | None = 200,
    state: str = "all",
    queue_path: Path | None = None,
    ledger_path: Path | None = None,
    include_nominations: bool = False,
    nominations_path: Path | None = None,
    display_root: Path | None = None,
) -> dict[str, Any]:
    """List queued memories with their current decision, without any text.

    AI nominations are opt-in: the automatic adjudicator reads the default
    listing, and a nomination must wait for Local Owner, never for automation.
    """
    queue = read_queue(queue_path)
    decisions = read_decisions(ledger_path)
    reviewable = list(queue["items"])
    nominations: dict[str, dict[str, Any]] = {}
    nominations_error = ""
    if include_nominations:
        try:
            nominations = _load_nominations(nominations_path)
        except MemoryReviewError as exc:
            nominations_error = str(exc)
        queued_ids = {item["memory_id"] for item in reviewable}
        reviewable = [
            {**item, "nomination": nominations[item["memory_id"]]["nomination"]}
            if nominations.get(item["memory_id"], {}).get("content_sha256")
            == item["content_sha256"] else item
            for item in reviewable
        ]
        # A memory already in the audit queue keeps the audit's fingerprint.
        reviewable.extend(
            item for memory_id, item in nominations.items() if memory_id not in queued_ids
        )
    if display_root is not None:
        from .presentation_scope import for_runtime
        scope = for_runtime(display_root)
        try:
            if scope.invalid or not scope.unchanged():
                raise ValueError("display_scope_unavailable")
            reviewable = [row for row in reviewable if not scope.hidden_memory(row["memory_id"], row) and not scope.hidden(row)]
            if scope.enabled:
                from .task_closure import personal_scope
                reviewable = [row for row in reviewable if personal_scope.memory_metadata_admitted(
                    row['memory_id'], read_memory_metadata(row['memory_id']), scope.memory_sources)]
            if not scope.unchanged() or not scope.memory_unchanged():
                raise ValueError("display_scope_unavailable")
        except ValueError as error:
            raise MemoryReviewError(str(error)) from None
    items: list[dict[str, Any]] = []
    for item in reviewable:
        key = (item["memory_id"], item["content_sha256"])
        decision = decisions.get(key)
        keep_active_current = bool(
            decision
            and decision["decision"] == "keep_active"
            and decision.get("policy_version") == item["policy_version"]
            and decision.get("candidate_fingerprint")
            == item["candidate_fingerprint"]
        )
        stale_decision = bool(
            decision and decision["decision"] == "keep_active" and not keep_active_current
        )
        decision_current = bool(
            decision
            and (decision["decision"] != "keep_active" or keep_active_current)
        )
        entry = {
            **item,
            "fingerprint": item["content_sha256"][:12],
            "decision": decision["decision"] if decision else "",
            "decision_title": ACTION_TITLES.get(decision["decision"], "") if decision else "",
            "lifecycle_status": decision["lifecycle_status"] if decision else "",
            "decided_at": decision["applied_at"] if decision else "",
            "review_state": "decided" if decision_current else "pending",
            "stale_decision": stale_decision,
            "retrievable": keep_active_current,
        }
        if state == "pending" and entry["review_state"] != "pending":
            continue
        if state == "decided" and entry["review_state"] != "decided":
            continue
        items.append(entry)
    returned_items = items if limit is None else items[: max(int(limit), 0)]
    result = {
        "interface": "memory-review",
        "contract": 2,
        "generated_at": queue["generated_at"],
        "queue_age_hours": queue["queue_age_hours"],
        "source_sha256": queue["source_sha256"],
        "total": len(reviewable),
        "returned": len(returned_items),
        "items": returned_items,
        "raw_access": False,
    }
    if include_nominations:
        result["queue_total"] = sum(row.get("source") != "ai_nomination" for row in reviewable)
        result["nomination_total"] = sum(row["memory_id"] in nominations for row in reviewable)
        if nominations_error:
            result["nominations_error"] = nominations_error
    return result


def memory_review_status(
    *,
    queue_path: Path | None = None,
    ledger_path: Path | None = None,
    nominations_path: Path | None = None,
    display_root: Path | None = None,
) -> dict[str, Any]:
    """Summarize the backlog and the artifacts retrieval depends on."""
    ledger = Path(ledger_path) if ledger_path else DEFAULT_LEDGER_PATH
    queue = Path(queue_path) if queue_path else DEFAULT_QUEUE_PATH
    result: dict[str, Any] = {
        "interface": "memory-review",
        "contract": 2,
        "queue_path_present": queue.exists(),
        "ledger_path_present": ledger.exists(),
        "memory_store_present": DEFAULT_CHROMA_PATH.exists(),
        "automatic_delete": False,
        "supersede_supported": False,
    }
    try:
        candidates = memory_review_candidates(
            limit=None, queue_path=queue_path, ledger_path=ledger_path,
            include_nominations=True, nominations_path=nominations_path,
            display_root=display_root,
        )
    except MemoryReviewError as exc:
        result["ok"] = False
        result["error"] = str(exc)
        # Retrieval fails closed on a missing/broken queue, so surface it loudly.
        result["retrieval_blocked"] = True
        return result
    queued = [item for item in candidates["items"] if item.get("source") != "ai_nomination"]
    counts: dict[str, int] = {}
    for item in queued:
        label = item["decision"] or "pending"
        counts[label] = counts.get(label, 0) + 1
    result["ok"] = True
    result["generated_at"] = candidates["generated_at"]
    result["queue_age_hours"] = candidates["queue_age_hours"]
    result["total"] = candidates["queue_total"]
    result["pending"] = counts.get("pending", 0)
    result["stale_decisions"] = sum(1 for item in queued if item.get("stale_decision"))
    nominated = [item for item in candidates["items"] if item.get("nomination")]
    result["nominations_total"] = len(nominated)
    result["nominations_pending"] = sum(
        1 for item in nominated if item["review_state"] == "pending"
    )
    if candidates.get("nominations_error"):
        result["nominations_error"] = candidates["nominations_error"]
    result["by_decision"] = counts
    result["retrieval_blocked"] = False
    return result


def _validate_decisions(
    decisions: list[dict[str, Any]],
    queue: dict[str, Any],
    nominations: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not decisions:
        raise MemoryReviewError("a batch needs at least one decision")
    if len(decisions) > MAX_BATCH_ITEMS:
        raise MemoryReviewError(f"a batch holds at most {MAX_BATCH_ITEMS} decisions")
    # Audit candidates win over nominations so retrieval sees one fingerprint.
    queued = {**(nominations or {}), **{item["memory_id"]: item for item in queue["items"]}}
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for ordinal, entry in enumerate(decisions, start=1):
        memory_id = str(entry.get("memory_id") or "").strip()
        action = str(entry.get("action") or "").strip()
        note = str(entry.get("note") or "").strip()
        if memory_id in seen:
            raise MemoryReviewError(f"duplicate decision for {memory_id}")
        if action not in ACTIONS:
            raise MemoryReviewError(f"unsupported action: {action or '(empty)'}")
        if memory_id not in queued:
            raise MemoryReviewError(f"memory is not in the current queue: {memory_id}")
        queue_item = queued[memory_id]
        content_sha256 = str(
            entry.get("content_sha256") or queue_item["content_sha256"]
        ).strip().lower()
        if content_sha256 != queue_item["content_sha256"]:
            # The audit hashed different text; an old judgement cannot carry over.
            raise MemoryReviewError(f"memory content changed since the audit: {memory_id}")
        seen.add(memory_id)
        nominated = (nominations or {}).get(memory_id, {})
        nomination = (
            nominated.get("nomination")
            if nominated.get("content_sha256") == content_sha256 else None
        )
        if not note and nomination:
            note = f"AI 提名（{nomination['nominator_agent']}）：{nomination['note']}"
        validated.append(
            {
                "ordinal": ordinal,
                "memory_id": memory_id,
                "content_sha256": content_sha256,
                "candidate_fingerprint": queue_item["candidate_fingerprint"],
                "policy_version": queue_item["policy_version"],
                "action": action,
                "note": note,
                "source": queue_item.get("source", "audit"),
                "nomination": nomination,
            }
        )
    return validated


def _verify_nomination_sources(
    items: list[dict[str, Any]],
    *,
    chroma_path: Path | None,
    collection_name: str,
) -> None:
    """A nomination stands only while its target and evidence are unchanged."""
    for item in items:
        if item["source"] != "ai_nomination":
            continue
        current = read_memory_text(
            item["memory_id"], chroma_path=chroma_path, collection_name=collection_name
        )
        if current["content_sha256"] != item["content_sha256"]:
            raise MemoryReviewError(f"record_changed:{item['memory_id']}")
        for evidence in item["nomination"]["evidence"]:
            if evidence.get("type") != "superseded_by":
                continue
            replacement = read_memory_text(
                evidence["memory_id"], chroma_path=chroma_path, collection_name=collection_name
            )
            if replacement["content_sha256"] != evidence["content_sha256"]:
                raise MemoryReviewError(f"evidence_changed:{evidence['memory_id']}")


def _superseding_memory_id(item: dict[str, Any]) -> str | None:
    for evidence in (item.get("nomination") or {}).get("evidence", []):
        if evidence.get("type") == "superseded_by":
            return str(evidence["memory_id"])
    return None


def _preview_digest(items: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        [
            [
                item["ordinal"], item["memory_id"], item["content_sha256"],
                item["candidate_fingerprint"], item["policy_version"], item["action"],
            ]
            for item in items
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def preview_review_batch(
    decisions: list[dict[str, Any]],
    *,
    queue_path: Path | None = None,
    ledger_path: Path | None = None,
    include_nominations: bool = False,
    nominations_path: Path | None = None,
    chroma_path: Path | None = None,
    collection_name: str = DEFAULT_COLLECTION_NAME,
) -> dict[str, Any]:
    """Validate a batch and return the digest ``apply`` will demand."""
    queue = read_queue(queue_path)
    nominations = _load_nominations(nominations_path) if include_nominations else None
    items = _validate_decisions(decisions, queue, nominations)
    _verify_nomination_sources(items, chroma_path=chroma_path, collection_name=collection_name)
    applied = read_decisions(ledger_path)
    summary: dict[str, int] = {}
    changes: list[dict[str, Any]] = []
    for item in items:
        summary[item["action"]] = summary.get(item["action"], 0) + 1
        previous = applied.get((item["memory_id"], item["content_sha256"]))
        changes.append(
            {
                "ordinal": item["ordinal"],
                "memory_id": item["memory_id"],
                "fingerprint": item["content_sha256"][:12],
                "previous_decision": previous["decision"] if previous else "",
                "action": item["action"],
                "action_title": ACTION_TITLES[item["action"]],
                "becomes_retrievable": item["action"] == "keep_active",
                "source": item["source"],
                **(
                    {
                        "nominated_by": item["nomination"]["nominator_agent"],
                        "suggested_action": item["nomination"]["suggested_action"],
                        "superseding_memory_id": _superseding_memory_id(item),
                    }
                    if item["nomination"] else {}
                ),
            }
        )
    return {
        "interface": "memory-review",
        "contract": 2,
        "preview_digest": _preview_digest(items),
        "item_count": len(items),
        "by_action": summary,
        "changes": changes,
        "confirmation": f"APPLY {_preview_digest(items)}",
        "raw_access": False,
    }


def _init_ledger_schema(connection: sqlite3.Connection) -> None:
    """Create the shared tables if the ledger has never been written."""
    actions = ",".join(f"'{value}'" for value in LIFECYCLE_BY_ACTION)
    connection.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS review_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            previous_action TEXT,
            action TEXT NOT NULL CHECK(action IN ({actions})),
            batch_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            candidate_fingerprint TEXT,
            policy_version TEXT
        );
        CREATE TABLE IF NOT EXISTS lifecycle_overrides (
            memory_id TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            lifecycle_status TEXT NOT NULL,
            decision TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            candidate_fingerprint TEXT,
            policy_version TEXT,
            PRIMARY KEY (memory_id, content_sha256)
        );
        CREATE TABLE IF NOT EXISTS batches (
            batch_id TEXT PRIMARY KEY,
            preview_digest TEXT NOT NULL CHECK(length(preview_digest) = 64),
            status TEXT NOT NULL CHECK(status IN ('previewed','confirmed','applying','applied','failed')),
            created_at TEXT NOT NULL,
            confirmed_at TEXT,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS batch_items (
            batch_id TEXT NOT NULL REFERENCES batches(batch_id),
            ordinal INTEGER NOT NULL,
            memory_id TEXT NOT NULL,
            content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
            action TEXT NOT NULL CHECK(action IN ({actions})),
            superseding_memory_id TEXT,
            note TEXT NOT NULL,
            supersede_draft_json TEXT,
            reviewer TEXT NOT NULL,
            decided_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','applying','applied','failed')),
            idempotency_key TEXT NOT NULL UNIQUE,
            error_code TEXT,
            applied_at TEXT,
            candidate_fingerprint TEXT,
            policy_version TEXT,
            PRIMARY KEY (batch_id, ordinal)
        );
        """
    )
    for table in ("review_events", "lifecycle_overrides", "batch_items"):
        columns = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if "candidate_fingerprint" not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN candidate_fingerprint TEXT"
            )
        if "policy_version" not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN policy_version TEXT")
    connection.execute("PRAGMA user_version=3")


def apply_review_batch(
    decisions: list[dict[str, Any]],
    *,
    confirm_digest: str,
    queue_path: Path | None = None,
    ledger_path: Path | None = None,
    chroma_path: Path | None = None,
    collection_name: str = DEFAULT_COLLECTION_NAME,
    include_nominations: bool = False,
    nominations_path: Path | None = None,
) -> dict[str, Any]:
    """Write a previewed batch to the shared ledger in one transaction.

    ``confirm_digest`` must equal the digest returned by ``preview_review_batch``
    for the same decisions, so a stale UI cannot apply a batch the reviewer
    never saw.
    """
    queue = read_queue(queue_path)
    nominations = _load_nominations(nominations_path) if include_nominations else None
    items = _validate_decisions(decisions, queue, nominations)
    digest = _preview_digest(items)
    if str(confirm_digest).strip() != digest:
        raise MemoryReviewError("confirmation digest does not match the previewed batch")
    _verify_nomination_sources(items, chroma_path=chroma_path, collection_name=collection_name)

    # Re-read the live record after re-reading the queue. A preview is not an
    # authorization credential, and neither the queue hash nor an old decision
    # may be applied after the underlying text changed.
    for item in items:
        current = read_memory_text(
            item["memory_id"],
            chroma_path=chroma_path,
            collection_name=collection_name,
        )
        if current["content_sha256"] != item["content_sha256"]:
            raise MemoryReviewError(f"record_changed:{item['memory_id']}")

    path = Path(ledger_path) if ledger_path else DEFAULT_LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    batch_id = uuid.uuid4().hex
    now = _utc_now()

    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    transaction_started = False
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        _init_ledger_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True
        # A repeated confirmation of the same current decisions must not add
        # duplicate audit events. Read after obtaining the writer lease so two
        # simultaneous submissions cannot both claim the same prior state.
        previous = read_decisions(ledger_path)
        if all(
            (prior := previous.get((item["memory_id"], item["content_sha256"])))
            and prior["decision"] == item["action"]
            and prior.get("candidate_fingerprint") == item["candidate_fingerprint"]
            and prior.get("policy_version") == item["policy_version"]
            for item in items
        ):
            connection.execute("COMMIT")
            transaction_started = False
            return {"interface":"memory-review", "contract":2, "applied_count":0,
                    "unchanged_count":len(items), "failed_count":0,
                    "by_action":{}, "raw_access":False}
        connection.execute(
            "INSERT INTO batches(batch_id, preview_digest, status, created_at, confirmed_at)"
            " VALUES (?, ?, 'applying', ?, ?)",
            (batch_id, digest, now, now),
        )
        for item in items:
            connection.execute(
                """
                INSERT INTO batch_items(
                    batch_id, ordinal, memory_id, content_sha256, action, note,
                    reviewer, decided_at, status, idempotency_key, applied_at
                    , candidate_fingerprint, policy_version, superseding_memory_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    item["ordinal"],
                    item["memory_id"],
                    item["content_sha256"],
                    item["action"],
                    item["note"],
                    REVIEWER,
                    now,
                    f"{batch_id}:{item['ordinal']}",
                    now,
                    item["candidate_fingerprint"],
                    item["policy_version"],
                    _superseding_memory_id(item),
                ),
            )
            prior = previous.get((item["memory_id"], item["content_sha256"]))
            connection.execute(
                """
                INSERT INTO review_events(
                    memory_id, content_sha256, previous_action, action, batch_id,
                    occurred_at, candidate_fingerprint, policy_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["memory_id"],
                    item["content_sha256"],
                    prior["decision"] if prior else None,
                    item["action"],
                    batch_id,
                    now,
                    item["candidate_fingerprint"],
                    item["policy_version"],
                ),
            )
            connection.execute(
                """
                INSERT INTO lifecycle_overrides(
                    memory_id, content_sha256, lifecycle_status, decision, applied_at,
                    batch_id, candidate_fingerprint, policy_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id, content_sha256) DO UPDATE SET
                    lifecycle_status = excluded.lifecycle_status,
                    decision = excluded.decision,
                    applied_at = excluded.applied_at,
                    batch_id = excluded.batch_id,
                    candidate_fingerprint = excluded.candidate_fingerprint,
                    policy_version = excluded.policy_version
                """,
                (
                    item["memory_id"],
                    item["content_sha256"],
                    LIFECYCLE_BY_ACTION[item["action"]],
                    item["action"],
                    now,
                    batch_id,
                    item["candidate_fingerprint"],
                    item["policy_version"],
                ),
            )
        connection.execute(
            "UPDATE batches SET status = 'applied', completed_at = ? WHERE batch_id = ?",
            (now, batch_id),
        )
        connection.execute("COMMIT")
        transaction_started = False
    except sqlite3.Error as exc:
        if transaction_started:
            connection.execute("ROLLBACK")
        raise MemoryReviewError(f"ledger write failed: {exc}") from exc
    finally:
        connection.close()
    os.chmod(path, 0o600)

    summary: dict[str, int] = {}
    for item in items:
        summary[item["action"]] = summary.get(item["action"], 0) + 1
    return {
        "interface": "memory-review",
        "contract": 2,
        "batch_id": batch_id,
        "applied_count": len(items),
        "failed_count": 0,
        "by_action": summary,
        "applied_at": now,
        "raw_access": False,
    }
